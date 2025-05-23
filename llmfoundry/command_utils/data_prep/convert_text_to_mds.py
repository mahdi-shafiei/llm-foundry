# Copyright 2024 MosaicML LLM Foundry authors
# SPDX-License-Identifier: Apache-2.0

import json
import logging
import math
import os
import tempfile
from concurrent.futures import ProcessPoolExecutor
from functools import partial
from glob import glob
from typing import Any, Iterable, Optional, cast

import numpy as np
from composer.utils import (
    ObjectStore,
    maybe_create_object_store_from_uri,
    parse_uri,
)
from numpy.typing import NDArray
from streaming import MDSWriter
from tqdm import tqdm
from transformers import AutoTokenizer, PreTrainedTokenizerBase

from llmfoundry.data.data import AbstractConcatTokensDataset
from llmfoundry.utils.data_prep_utils import (
    DownloadingIterable,
    download_file,
    merge_shard_groups,
)
from llmfoundry.utils.exceptions import (
    CannotUnicodeDecodeFile,
    DatasetTooSmallError,
    InputFolderMissingDataError,
    InputFolderNotFound,
    OutputFolderNotEmptyError,
)

log = logging.getLogger(__name__)

DONE_FILENAME = '.text_to_mds_conversion_done'


class ConcatTokensFromFilesDataset(AbstractConcatTokensDataset):
    """An IterableDataset that returns token samples for MDSWriter from files.

    Returns dicts of {'tokens': ndarray:int32}

    Each file is considered a sequence.
    """

    def __init__(
        self,
        files: Iterable[str],
        tokenizer: PreTrainedTokenizerBase,
        max_length: int,
        bos_text: str,
        eos_text: str,
        no_wrap: bool,
    ):
        self.files = files
        super().__init__(tokenizer, max_length, bos_text, eos_text, no_wrap)
        log.info(f'Initialized ConcatTokensFromFilesDataset.')

    def __iter__(self) -> Iterable[dict[str, NDArray]]:
        log.info(
            'Starting iteration over files in ConcatTokensFromFilesDataset',
        )
        buffer = []
        for file in self.files:
            log.info(f'Processing file: {file}')
            with open(file, 'r') as f:
                buffer += self.bos_tokens
                first_chunk = True
                # Read the file in 1MB chunks to avoid memory issues
                try:
                    for chunk in iter(partial(f.read, 1000000), ''):
                        # Tokenize the chunk
                        encoded = self.tokenizer(
                            chunk,
                            truncation=False,
                            padding=False,
                        )
                        iids = cast(Any, encoded['input_ids'])

                        # If this is not the first chunk, remove the BOS token
                        if not first_chunk:
                            if iids[0] == self.tokenizer.bos_token_id:
                                iids = iids[1:]

                        # Add the tokens to the buffer
                        buffer += iids
                        while len(buffer) >= self.max_length:
                            concat_sample = buffer[:self.max_length]
                            buffer = buffer[self.max_length:
                                           ] if self.should_wrap else []
                            yield {
                                'tokens':
                                    np.asarray(concat_sample, dtype=np.int32),
                            }

                        first_chunk = False
                except UnicodeDecodeError:
                    raise CannotUnicodeDecodeFile(text_file=file)

                # Add the EOS token to the buffer to separate files.
                buffer += self.eos_tokens

        # Yield any remaining samples of size max_length.
        while len(buffer) >= self.max_length:
            concat_sample = buffer[:self.max_length]
            buffer = buffer[self.max_length:] if self.should_wrap else []
            yield {'tokens': np.asarray(concat_sample, dtype=np.int32)}

        log.info(
            'Finished iterating over files in ConcatTokensFromFilesDataset',
        )


def get_object_names(input_folder: str) -> list[str]:
    """Get object names from a local or remote folder.

    Args:
        input_folder (str): local or remote folder path.
    """
    object_store = maybe_create_object_store_from_uri(input_folder)
    if object_store is not None:
        _, _, folder_prefix = parse_uri(input_folder)
        try:
            names = [
                name for name in object_store.list_objects(folder_prefix)
                if name.endswith('.txt')
            ]
            log.info(f'Found {len(names)} text files in remote storage')
        except FileNotFoundError:
            raise InputFolderNotFound(folder_prefix)

    else:
        # input_folder is a local folder
        names = [
            text_file for dirpath, _, _ in os.walk(input_folder)
            for text_file in glob(os.path.join(dirpath, '*.txt'))
        ]
    # return names, sizes
    log.info(f'Found {len(names)} text files at {input_folder}')

    return names


def get_task_args(
    object_names: list[str],
    output_root: str,
    input_folder: str,
    n_groups: int,
    tokenizer_name: str,
    concat_tokens: int,
    eos_text: str,
    bos_text: str,
    no_wrap: bool,
    compression: str,
    trust_remote_code: bool,
) -> Iterable:
    """Get download_and_convert arguments split across n_groups.

    Each group handles a portion of object_names.

    Args:
        object_names (List[str]): Names of objects to process
        output_root (str): Folder to write MDS shards to
        input_folder (str): Folder of text files to process
        n_groups (int): Number of groups to split the object names into
        tokenizer_name (str): Name of tokenizer to use
        concat_tokens (int): Concatenate up to this many tokens
        eos_text (str): Text to append to each example to separate concatenated samples
        bos_text (str): Text to prepend to each example to separate concatenated samples
        no_wrap: (bool): Whether to let text examples wrap across multiple training examples
        compression (str): The compression algorithm to use for MDS writing
        trust_remote_code (bool): If true, allows custom code to be executed to load the tokenizer
    """
    log.info(
        f'Preparing task arguments for {len(object_names)} objects across {n_groups} groups',
    )
    num_objects = len(object_names)
    objs_per_group = math.ceil(num_objects / n_groups)
    for group, i in enumerate(range(0, num_objects, objs_per_group)):
        output_subdir = os.path.join(output_root, str(group))
        log.info(
            f'Created task for group {group} with {min(objs_per_group, num_objects - i)} objects',
        )
        yield (
            object_names[i:min(i + objs_per_group, num_objects)],
            output_subdir,
            input_folder,
            tokenizer_name,
            concat_tokens,
            eos_text,
            bos_text,
            no_wrap,
            compression,
            trust_remote_code,
        )


def download_and_convert_starargs(args: tuple):
    """Helper function to call download_and_convert with star args.

    This helps us use download_and_convert with multiprocessing.
    """
    return download_and_convert(*args)


def download_and_convert(
    file_names: list[str],
    output_folder: str,
    input_folder: str,
    tokenizer_name: str,
    concat_tokens: int,
    eos_text: str,
    bos_text: str,
    no_wrap: bool,
    compression: str,
    trust_remote_code: bool,
):
    """Downloads and converts text files to MDS format.

    Args:
        file_names (List[str]): Files to process
        output_folder (str): Folder to write MDS shards to
        input_folder (str): Folder of text files to process
        tokenizer_name (str): Name of tokenizer to use
        concat_tokens (int): Concatenate up to this many tokens
        eos_text (str): Text to append to each example to separate concatenated samples
        bos_text (str): Text to prepend to each example to separate concatenated samples
        no_wrap: (bool): Whether to let text examples wrap across multiple training examples
        compression (str): The compression algorithm to use for MDS writing
        trust_remote_code (bool): If true, allows custom code to be executed to load the tokenizer
    """
    log.info(f'Starting download and conversion for {len(file_names)} files')

    object_store = maybe_create_object_store_from_uri(input_folder)

    # Download file_names
    downloading_iter = DownloadingIterable(
        object_names=file_names,
        output_folder=None, # Downloads to temporary files.
        object_store=object_store,
    )
    log.info(f'Initializing tokenizer: {tokenizer_name}')
    tokenizer = AutoTokenizer.from_pretrained(
        tokenizer_name,
        trust_remote_code=trust_remote_code,
    )
    tokenizer.model_max_length = 5000000000  # Hack to prevent warnings from HuggingFace

    # Use the ConcatTokensDataset from LLM-foundry to concatenate sequences of tokens up
    # to the maximum sequence length
    dataset = ConcatTokensFromFilesDataset(
        files=downloading_iter,
        max_length=concat_tokens,
        tokenizer=tokenizer,
        eos_text=eos_text,
        bos_text=bos_text,
        no_wrap=no_wrap,
    )

    columns = {'tokens': 'ndarray:int32'}

    log.info('Converting to MDS format...')
    with MDSWriter(
        out=output_folder,
        columns=columns,
        compression=compression,
    ) as out:
        for sample in tqdm(dataset):
            out.write(sample)

    log.info(f'Completed download and conversion for {len(file_names)} files')


def is_remote_path(path: str) -> bool:
    """Checks whether a path is a remote path.

    Args:
        path (str): path to check
    """
    backend, _, _ = parse_uri(path)
    return backend != ''


def is_already_processed(
    output_root: str,
    args_str: str,
    object_names: list[str],
) -> bool:
    """Determines whether a group of text files has already been processed.

    Checks the done fie at output root to determine this.

    Args:
        output_root (str): Output folder where a done file may exist
        args_str (str): String representation of the arguments
        object_names (List[str]): Names of objects to convert to MDS format
    """
    log.info(
        f'Checking if {len(object_names)} objects have already been processed in {output_root}',
    )

    # Retrieve the done file contents
    output_object_store = maybe_create_object_store_from_uri(output_root)
    if output_object_store is not None:
        # Download and read the done file from the remote object store
        _, _, output_folder_prefix = parse_uri(output_root)
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                done_file = os.path.join(tmp_dir, DONE_FILENAME)
                download_file(
                    object_store=output_object_store,
                    object_name=os.path.join(
                        output_folder_prefix,
                        DONE_FILENAME,
                    ),
                    output_filename=done_file,
                )
                with open(done_file) as df:
                    done_file_contents = df.read().splitlines()
                log.info(f'Retrieved done file contents from remote storage')
        except FileNotFoundError:
            log.info('Done file not found in remote storage')
            return False
    else:
        # Read the local done file
        done_file = os.path.join(output_root, DONE_FILENAME)
        if not os.path.isfile(done_file):
            log.info('Done file not found in local storage')
            return False
        with open(done_file) as df:
            done_file_contents = df.read().splitlines()
        log.info(f'Retrieved done file contents from local storage')

    # Compare the arguments
    prev_args_str = done_file_contents[0]
    if prev_args_str != args_str:
        log.info('Arguments have changed, reprocessing required')
        return False

    # Compare file names
    prev_names = done_file_contents[1:]
    if len(prev_names) != len(object_names):
        log.info('Number of files has changed, reprocessing required')
        return False
    for idx, prev_name in enumerate(prev_names):
        if object_names[idx] != prev_name:
            log.info('File names have changed, reprocessing required')
            return False

    log.info('All files have already been processed')
    return True


def write_done_file(folder: str, args_str: str, object_names: list[str]):
    """Write a file to signify completion.

    This the done file includes the arguments to processing and
    a list of objects that were processed.

    Args:
        folder (str): Folder to write the done file to
        args_str (str): String representation of arguments
        object_names (List[str]): List of objects to convert to MDS format
    """
    with open(os.path.join(folder, DONE_FILENAME), 'w') as done_file:
        log.info(f'Writing done file.')
        done_file.write('\n'.join([args_str] + object_names) + '\n')
    log.info(f'Done file written successfully')


def convert_text_to_mds(
    tokenizer_name: str,
    output_folder: str,
    input_folder: str,
    concat_tokens: int,
    eos_text: str,
    bos_text: str,
    no_wrap: bool,
    compression: str,
    processes: int,
    args_str: str,
    reprocess: bool,
    trust_remote_code: bool,
):
    """Convert a folder of text files to MDS format.

    Args:
        tokenizer_name (str): Name of tokenizer to use
        output_folder (str): Folder to write MDS shards to
        input_folder (str): Folder of text files to process
        concat_tokens (int): Concatenate up to this many tokens
        eos_text (str): Text to append to each example to separate concatenated samples
        bos_text (str): Text to prepend to each example to separate concatenated samples
        no_wrap: (bool): Whether to let text examples wrap across multiple training examples
        compression (str): The compression algorithm to use for MDS writing
        processes (int): The number of processes to use.
        args_str (str): String representation of the arguments
        reprocess (bool): Whether to always reprocess the given folder of text files
        trust_remote_code (bool): If true, allows custom code to be executed to load the tokenizer
    """
    # Load the tokenizer once on the main process so that the files are cached to avoid race conditions
    # in the Hugging Face load code
    AutoTokenizer.from_pretrained(
        tokenizer_name,
        trust_remote_code=trust_remote_code,
    )

    is_remote_output = is_remote_path(output_folder)
    log.info(f'Output is remote: {is_remote_output}')

    object_names = get_object_names(input_folder)
    if len(object_names) == 0:
        log.error(f'No text files found in input folder: {input_folder}')
        raise InputFolderMissingDataError(input_folder)

    # Check if the text files in the bucket have already been processed.
    if not reprocess and is_already_processed(
        output_folder,
        args_str,
        object_names,
    ):
        log.info(
            f'Input folder {input_folder} is already processed at {output_folder} and '
            +
            'reprocess is set to False. Set reprocess to True if you would like to force reprocessing.',
        )
        return

    # Use a temporary local directory if the output is remote and there are more than 1 processes
    local_output_folder = tempfile.TemporaryDirectory(
    ).name if is_remote_output else output_folder
    log.info(f'Using local output folder: {local_output_folder}')

    if os.path.isdir(output_folder) and len(os.listdir(output_folder)) > 0:
        log.error(f'Output folder is not empty: {output_folder}')
        raise OutputFolderNotEmptyError(output_folder)

    if processes > 1:
        log.info(f'Using multiprocessing with {processes} processes')
        # Download and convert the text files in parallel
        args = get_task_args(
            object_names,
            local_output_folder,
            input_folder,
            processes,
            tokenizer_name,
            concat_tokens,
            eos_text,
            bos_text,
            no_wrap,
            compression,
            trust_remote_code,
        )
        with ProcessPoolExecutor(max_workers=processes) as executor:
            list(executor.map(download_and_convert_starargs, args))

        log.info('Merging MDS shards from each process')
        # Merge the mds shards from each of the processes into a single folder
        merge_shard_groups(local_output_folder)
    else:
        log.info('Using single process for download and conversion')
        download_and_convert(
            object_names,
            local_output_folder,
            input_folder,
            tokenizer_name,
            concat_tokens,
            eos_text,
            bos_text,
            no_wrap,
            compression,
            trust_remote_code,
        )

    index_path = os.path.join(local_output_folder, 'index.json')
    with open(index_path, 'r') as index_file:
        if not json.load(index_file)['shards']:
            raise DatasetTooSmallError(
                reason='No shards were created when converting text to MDS.',
            )

    # Write a done file with the args and object names
    write_done_file(local_output_folder, args_str, object_names)

    if is_remote_output:
        # Upload the local output to the remote location
        output_object_store = cast(
            ObjectStore,
            maybe_create_object_store_from_uri(output_folder),
        )
        _, _, output_folder_prefix = parse_uri(output_folder)
        files_to_upload = os.listdir(local_output_folder)

        for file in files_to_upload:
            assert not os.path.isdir(file)
            remote_path = os.path.join(output_folder_prefix, file)
            output_object_store.upload_object(
                remote_path,
                os.path.join(local_output_folder, file),
            )


def _configure_logging(logging_level: str):
    """Configure logging.

    Args:
        logging_level (str): Logging level.
    """
    logging.basicConfig(
        format=
        f'%(asctime)s: [%(process)d][%(threadName)s]: %(levelname)s: %(name)s: %(message)s',
        force=True,
    )
    logging_level = logging_level.upper()
    logging.getLogger('llmfoundry').setLevel(logging_level)
    logging.getLogger(__name__).setLevel(logging_level)
    log.info(f'Logging level set to {logging_level}')


def convert_text_to_mds_from_args(
    output_folder: str,
    input_folder: str,
    compression: str,
    concat_tokens: int,
    tokenizer_name: str,
    bos_text: Optional[str],
    eos_text: Optional[str],
    use_tokenizer_eos: bool,
    no_wrap: bool,
    processes: int,
    reprocess: bool,
    trust_remote_code: bool,
    logging_level: str,
) -> None:
    """A wrapper for `convert_text_to_mds` to parse arguments.

    Args:
        output_folder (str): Folder to write MDS shards to
        input_folder (str): Folder of text files to process
        compression (str): The compression algorithm to use for MDS writing
        concat_tokens (int): Concatenate up to this many tokens
        tokenizer_name (str): The name of the tokenizer to use
        bos_text (Optional[str]): The text to prepend to each example to separate concatenated examples
        eos_text (Optional[str]): The text to append to each example to separate concatenated examples
        use_tokenizer_eos (bool): Use the EOS text from the tokenizer
        no_wrap (bool): Whether to let text examples wrap across multiple training examples
        processes (int): The number of processes to use to download and convert the dataset
        reprocess (bool): If true, reprocess the input_folder to MDS format. Otherwise, only reprocess upon changes to the input folder or dataset creation parameters.
        trust_remote_code (bool): If true, allows custom code to be executed to load the tokenizer
        logging_level (str): Logging level for the script. Default is INFO.

    Raises:
        ValueError: If `use_tokenizer_eos` is True and `eos_text` is not None
    """
    os.environ['WORLD_SIZE'] = '1'
    if use_tokenizer_eos:
        # Ensure that eos text is not specified twice.
        if eos_text is not None:
            ValueError(
                'Cannot set --eos_text with --use_tokenizer_eos. Please specify one.',
            )
        tokenizer = AutoTokenizer.from_pretrained(
            tokenizer_name,
            trust_remote_code=trust_remote_code,
        )
        eos_text = tokenizer.eos_token

    # now that we have validated them, change BOS/EOS to strings
    if bos_text is None:
        bos_text = ''
    if eos_text is None:
        eos_text = ''
    _configure_logging(logging_level)

    # Define args for _args_str
    args = {
        'tokenizer': tokenizer_name,
        'output_folder': output_folder,
        'input_folder': input_folder,
        'compression': compression,
        'concat_tokens': concat_tokens,
        'eos_text': eos_text,
        'bos_text': bos_text,
        'no_wrap': no_wrap,
        'processes': processes,
        'reprocess': reprocess,
        'trust_remote_code': trust_remote_code,
    }
    convert_text_to_mds(
        tokenizer_name=tokenizer_name,
        output_folder=output_folder,
        input_folder=input_folder,
        concat_tokens=concat_tokens,
        eos_text=eos_text,
        bos_text=bos_text,
        no_wrap=no_wrap,
        compression=compression,
        processes=processes,
        reprocess=reprocess,
        trust_remote_code=trust_remote_code,
        args_str=str(args),
    )
