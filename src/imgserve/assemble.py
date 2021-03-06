from __future__ import annotations
import logging
import os
import json
import shutil
from collections import defaultdict
from pathlib import Path
from tqdm import tqdm

from elasticsearch import Elasticsearch, helpers

from .elasticsearch import RAW_IMAGES_INDEX_PATTERN, all_field_values
from .errors import NoImagesInElasticsearchError, NoQueriesGatheredError
from .logger import simple_logger

"""
  Assemble image data
"""


def recursively_combine(
    field_values: Dict[str, List[str]], so_far: Optional[Dict[str, str]] = None
) -> List[Dict]:
    """
        create all possible queries to target each combination of field_values
    """
    if so_far is None:
        so_far = list()

    # pivot around a new field every iteration
    left_to_pivot = [field for field in field_values.keys() if field not in so_far]
    pivot_field = left_to_pivot.pop()
    if len(left_to_pivot) > 0:
        # we still have more fields to get through, recurse for each pivot_value to generate all combinations
        for pivot_value in field_values[pivot_field]:
            pivot_so_far = {pivot_field: pivot_value}
            pivot_so_far.update(so_far)
            yield from recursively_combine(field_values, so_far=pivot_so_far)
    else:
        # we have accumulated so_far and now have just one list to get through for the final field, can yield actual queries here
        shared_query_filter_parts = [
            {"term": {field: value}} for field, value in so_far.items()
        ]
        for pivot_value in field_values[pivot_field]:
            query = {
                "query": {
                    "bool": {
                        "filter": shared_query_filter_parts
                        + [{"term": {pivot_field: pivot_value}}]
                    }
                }
            }
            slug_parts = list()
            for term_filter in query["query"]["bool"]["filter"]:
                for field, value in term_filter["term"].items():
                    slug_parts.append(f"{field}={value}")
            slug = "|".join(slug_parts)
            yield (slug, query)


def assemble_downloads(
    elasticsearch_client: Elasticsearch,
    s3_client: botocore.clients.S3,
    bucket_name: str,
    trial_ids: List[str],
    experiment_name: str,
    dimensions: List[str],
    local_data_store: Path,
    dry_run: bool = False,
    force_remote_pull: bool = False,
    prompt: bool = True,
) -> Path:
    """
        Assemble a "downloads" folder for compsyn to run on.
        Data may already exist locally, or can be gathered from S3.
        In either case, Elasticsearch is used as the source of truth for gathering the required images.
    """
    log = simple_logger(
        "imgserve.assemble_downloads" + (f".DRY_RUN" if dry_run else "")
    )
    log.info("enumerating images from elasticsearch")

    downloads_path = local_data_store.joinpath(experiment_name).joinpath("downloads")

    global ELASTICSEARCH_CLIENT
    ELASTICSEARCH_CLIENT = elasticsearch_client

    # query elasticsearch to assemble a list of required images
    if len(trial_ids) >= 0:
        shared_filter = {"terms": {"trial_id": trial_ids}}
        field_values_query = {"query": {"bool": {"filter": [shared_filter]}}}
    else:
        log.info("no trial IDs passed, data will encompass all trial ids")
        shared_filter = None
        field_values_query = {"query": {"match_all": {}}}
    field_values = {
        field: list(
            all_field_values(elasticsearch_client, field, query=field_values_query)
        )
        for field in dimensions
    }
    errors = list()
    for field_key, field_value in field_values.items():
        if len(field_value) == 0:
            errors.append(
                f"Could not find any field values for {RAW_IMAGES_INDEX_PATTERN} matching the query: {json.dumps(field_values_query)}"
            )
    if len(errors) > 0:
        raise NoImagesInElasticsearchError("\n".join(errors))
    queries = [(slug, query) for (slug, query) in recursively_combine(field_values)]
    if len(queries) == 0:
        log.debug(f"{len(queries)} queries gathered for data extraction")
        raise NoQueriesGatheredError(
            f"no queries could be generated for field values {field_values}!"
        )

    image_directories: Dict[str, List[Path]] = dict()
    with tqdm(total=len(queries), desc="(step 1/2) Query") as pbar:
        for slug, query in queries:
            if shared_filter is not None:
                query["query"]["bool"]["filter"].append(shared_filter)
            paths = list()
            for image_doc in helpers.scan(
                ELASTICSEARCH_CLIENT, index=RAW_IMAGES_INDEX_PATTERN, query=query
            ):
                source = image_doc["_source"]
                try:
                    relative_image_path = (
                        Path("data")
                        .joinpath(source["trial_id"])
                        .joinpath(source["hostname"])
                        .joinpath(source["query"].replace(" ", "_"))
                        .joinpath(source["trial_timestamp"])
                        .joinpath("images")
                        .joinpath(source["image_id"])
                        .with_suffix(".jpg")
                    )
                    paths.append(relative_image_path)
                except KeyError as e:
                    print(image_doc)
                    print(e)
            image_directories[slug] = paths
            pbar.update(1)

    total_images = sum([len(image_paths) for image_paths in image_directories.values()])
    if total_images == 0:
        raise NoImagesInElasticsearchError(
            f"{json.dumps(queries, indent=2)}\n  0 images available for assembly from 'raw-images' according to the above query. Has this trial been indexed?"
        )

    if not dry_run:
        log.info("downloading images from S3")
        if downloads_path.is_dir():
            existing_at_path = list(downloads_path.iterdir())
            if len(existing_at_path) > 0:
                if prompt and input(
                    f"{len(existing_at_path)} items found at {downloads_path}, clear for this new run? (y/n) "
                ).lower() not in ["y", "yes"]:
                    log.warning(
                        "not clearing, new results will be mixed with existing data"
                    )
                else:
                    log.debug(
                        "clearing downloads path to make way for this experiment to run"
                    )
                    shutil.rmtree(downloads_path)
        downloads_path.mkdir(exist_ok=True, parents=True)
        with tqdm(total=total_images, desc="(step 2/2) Download") as pbar:
            for slug, image_paths in image_directories.items():
                images_directory = downloads_path.joinpath(slug)
                for image_path in image_paths:
                    # if we already have the .zip archive at this path, don't retrieve from s3
                    relative_path = image_path.relative_to("data")
                    archive_path = local_data_store.joinpath(
                        relative_path.parts[0]
                    ).joinpath(relative_path)
                    image_assembly_path = images_directory.joinpath(image_path.name)
                    image_assembly_path.parent.mkdir(exist_ok=True, parents=True)
                    if archive_path.is_file() and not force_remote_pull:
                        image_assembly_path.write_bytes(archive_path.read_bytes())
                    else:
                        image_obj = s3_client.get_object(
                            Bucket=bucket_name, Key=str(image_path)
                        )
                        archive_path.parent.mkdir(exist_ok=True, parents=True)
                        try:
                            archive_path.write_bytes(image_obj["Body"].read())
                        except botocore.exceptions.ReadTimeoutError as exc:
                            # TODO retry logic
                            log.error(f"S3 read timeout, skipping. {exc}")
                        images_directory.joinpath(image_path.name).write_bytes(
                            archive_path.read_bytes()
                        )
                    pbar.update(1)
    log.info(f"{total_images} image paths gathered")
    if not dry_run:
        log.info(f"assembled directory: {downloads_path}")
        return downloads_path
