#!/usr/bin/env python3
from __future__ import annotations
import hashlib
from pathlib import Path

import numpy as np
from compsyn.vectors import Vector

from .errors import NoDownloadsError, MalformedTagsError
from .logger import simple_logger


def tags_to_hash(tags: List[str]) -> str:
    m = hashlib.sha256()
    for tag in sorted(tags):
        m.update(tag.encode("utf-8"))
    return m.hexdigest()


def array_to_list(array: numpy.ndarray) -> List[float]:
    out = list()
    for elem in array:
        if np.isnan(elem):
            out.append(None)
        else:
            out.append(elem)
    return out


def get_vectors(
    downloads_path: Path,
) -> Generator[Tuple[Vector, Dict[str, Any]], None, None]:
    log = simple_logger("get_vectors")
    for folder in downloads_path.iterdir():
        if len(list(folder.iterdir())) == 0:
            raise NoDownloadsError(f"No downloaded images available at {folder}")
        vector = Vector(folder.name).load_from_folder(folder.parent)
        tags = str(folder.name).split("|")
        try:
            metadata = {key: value for key, value in (tag.split("=") for tag in tags)}
        except ValueError as e:
            log.error(f"Couldn't load metadata from colorgram stem: {tags}")
            continue
        metadata.update(
            {
                "downloads": [img.stem for img in folder.iterdir()],
                "s3_key": tags_to_hash(tags),
                "rgb_dist": array_to_list(vector.rgb_dist),
                "jzazbz_dist": array_to_list(vector.jzazbz_dist),
            }
        )
        try:
            metadata.update(rgb_dist_std=array_to_list(vector.rgb_dist_std))
        except AttributeError:
            pass

        try:
            metadata.update(jzazbz_dist_std=array_to_list(vector.jzazbz_dist_std))
        except AttributeError:
            pass

        yield vector, metadata
