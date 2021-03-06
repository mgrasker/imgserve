#!/usr/bin/env python3
from __future__ import annotations
import argparse
import base64
import copy
import csv
import os
import json
import logging
from collections import defaultdict
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.authentication import (
    AuthenticationBackend,
    AuthenticationError,
    SimpleUser,
    UnauthenticatedUser,
    AuthCredentials,
    requires,
)
from starlette.requests import Request
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    StreamingResponse,
    RedirectResponse,
)
from starlette.routing import Route, Mount, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates
from starlette.websockets import WebSocket

from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.middleware.cors import CORSMiddleware

from imgserve import get_experiment_csv_path, STATIC, LOCAL_DATA_STORE
from imgserve.api import Experiment
from imgserve.args import get_elasticsearch_args, get_s3_args
from imgserve.clients import get_clients
from imgserve.elasticsearch import get_response_value
from imgserve.logger import simple_logger

from vectors import get_experiments

log = simple_logger("api")

# Requests will be authenticated by an upstream component, in this case most likely an OAuth2 proxy that adds authentication headers
class BasicAuthBackend(AuthenticationBackend):
    async def authenticate(self, request):
        if "Authorization" not in request.headers:
            return

        auth = request.headers["Authorization"]
        try:
            scheme, credentials = auth.split()
            if scheme.lower() != "basic":
                return
            decoded = base64.b64decode(credentials).decode("ascii")
        except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
            raise AuthenticationError("Invalid basic auth credentials")

        username, _, password = decoded.partition(":")
        try:
            assert password == USERS[username]
        except (AssertionError, KeyError) as exc:
            raise AuthenticationError("Username or Password incorrect for {username}.")

        log.info(f"authenticated {username}")
        return AuthCredentials(["authenticated"]), SimpleUser(username)


def on_auth_error(request: Request, exc: Exception):
    return JSONResponse({"error": str(exc)}, status_code=401)


middleware = [
    Middleware(
        CORSMiddleware,
        allow_origins=[
            "comp-syn.ialcloud.xyz:443",
            "comp-syn.com:443",
            "localhost:8080",
        ],
        allow_headers=["*"],
        allow_methods=["*"],
    ),
    Middleware(
        AuthenticationMiddleware, backend=BasicAuthBackend(), on_error=on_auth_error
    ),
]

app = Starlette(middleware=middleware)
app.mount("/static", StaticFiles(directory=STATIC), name="static")

templates = Jinja2Templates(directory="templates")


USERS = {
    "compsyn": os.getenv("IMGSERVE_USER_COMPSYN_PASSWORD"),
    "admin": os.getenv("IMGSERVE_USER_ADMIN_PASSWORD"),
}


def get_raw_data_link(experiment: str) -> Optional[str]:
    link = Path(f"static/dropbox-links/{experiment}")
    if link.is_file():
        return link.read_text()
    else:
        return None


async def open_experiment_csv(csv_path: Path) -> Dict[str, Dict[str, Any]]:
    region_column = "region"
    query_terms_column = "search_term"
    # marshal around query terms
    unique_queries = dict()
    with open(csv_path, encoding="utf-8-sig", newline="") as csvf:
        for query in csv.DictReader(csvf, dialect="excel"):
            regions = query.pop(region_column).split(" ")
            search_term = query.pop(query_terms_column)
            for region in regions:
                if search_term in unique_queries:
                    unique_queries[search_term]["regions"].append(region.lower())
                else:
                    unique_queries[search_term] = {"regions": [region.lower()]}
                    unique_queries[search_term].update(**query)
    return unique_queries


async def respond_with_404(request: Request, message: str):
    response = templates.TemplateResponse(
        "404.html", {"request": request, "message": message,},
    )
    return response


@app.route("/")
@requires("authenticated")
async def home(request: Request):
    template = "home.html"

    experiments = get_experiments(ELASTICSEARCH_CLIENT, debug=DEBUG)
    results = [p.name for p in Path("static/img/colorgrams").glob("*")]

    context = {"request": request, "experiments": experiments, "results": results}
    return templates.TemplateResponse(template, context)


@app.route("/archive")
@requires("authenticated", redirect="homepage")
async def archive(request: Request):
    if "experiment" in request.query_params:
        experiment = request.query_params["experiment"]
        dl_link = get_raw_data_link(experiment)
        if dl_link is None:
            response = await respond_with_404(
                request=request, message=f"No download link available for {experiment}"
            )
        else:
            response = RedirectResponse(url=dl_link)
    else:
        template = "archive.html"
        experiments = get_experiments(ELASTICSEARH_CLIENT)
        context = {"request": request, "experiments": experiments}
        response = templates.TemplateResponse(template, context)

    return response


@app.route("/search")
@requires("authenticated", redirect="homepage")
async def search(request: Request):
    template = "search.html"

    experiments = get_experiments(ELASTICSEARCH_CLIENT)

    context = {"request": request, "experiments": experiments}
    return templates.TemplateResponse(template, context)


@app.route("/sketch")
@requires("authenticated", redirect="homepage")
async def sketch(request: Request):

    default_experiment = None
    try:
        default_experiment = request.query_params["default_experiment"]
    except KeyError:
        pass

    template = "sketch.html"

    experiments = get_experiments(ELASTICSEARCH_CLIENT)

    context = {
        "request": request,
        "default_experiment": default_experiment
        if default_experiment is not None
        else "concreteness",
        "experiments": experiments,
    }
    return templates.TemplateResponse(template, context)



@app.route("/search")
@requires("authenticated", redirect="homepage")
async def search(request: Request):

    template = "search.html"

    experiments = get_experiments(ELASTICSEARCH_CLIENT)

    context = {
        "request": request,
        "experiments": experiments
    }
    return templates.TemplateResponse(template, context)


@app.route("/image")
async def get_image(request: Request):
    image_id = request.query_params["image_id"]

    image_urls = [
        image_url
        for image_url in get_response_value(
            elasticsearch_client=ELASTICSEARCH_CLIENT,
            index="raw-images",
            query={
                "query": {
                    "bool": {
                        "filter": {"term": {"image_id": image_id}}
                    }
                },
                "aggregations": {
                    "image_url": {
                        "terms": {
                            "field": "image_url",
                            "size": 250,
                        }
                    }
                }
            },
            value_keys=["aggregations", "image_url", "buckets", "*", "key"],
            size=0,
            #debug=True,
        )
    ]

    s3_region_name = S3_CLIENT.meta._client_config.__dict__["region_name"]
    s3_bucket = "compsyn"
    try:
        cropped_face_urls = [
            f"https://{s3_bucket}.s3.{s3_region_name}.amazonaws.com/{key['experiment_name']}/faces/{key['face_id']}.jpg"
            for key in get_response_value(
                elasticsearch_client=ELASTICSEARCH_CLIENT,
                index="cropped-face*",
                query={
                    "query": {
                        "bool": {
                            "filter": {"term": {"image_id": image_id}}
                        }
                    },
                    "aggregations": {
                        "face_image": {
                            "composite": {
                                "size": 500,
                                "sources": [
                                    {"face_id": { "terms": { "field": "face_id" }}},
                                    {"experiment_name": { "terms": { "field": "experiment_name" }}},
                                ]
                            }
                        }
                    }
                },
                value_keys=["aggregations", "face_image", "buckets", "*", "key"],
                size=0,
                #debug=True,
                composite_aggregation_name="face_image"
            )
        ]
    except KeyError:
        cropped_face_urls = list()

    template = "image.html"
    context = {
        "request": request,
        "image_urls": image_urls,
        "cropped_face_urls": cropped_face_urls,
    }
    return templates.TemplateResponse(template, context)


async def valid_webhook_request(
    websocket: WebSocket, request: Dict[str, Any], required_keys: List[str]
) -> bool:
    valid = True
    missing = list()
    for required_key in required_keys:
        if required_key not in request:
            valid = False
            missing.append(required_key)

    if not valid:
        await websocket.send_json(
            {"status": 400, "message": "missing required keys", "missing": missing}
        )
    return valid


@app.websocket_route("/data")
async def experiments_listener(websocket: WebSocket):
    experiments = get_experiments(ELASTICSEARCH_CLIENT)

    await websocket.accept()
    request = await websocket.receive_json()
    if "single_value" not in request:
        request["single_value"] = True

    log.info("processing websocket request")

    if await valid_webhook_request(websocket, request, required_keys=["action"]):
        if request["action"] == "get":
            if await valid_webhook_request(
                websocket, request, required_keys=["experiment", "get"]
            ):
                if request["experiment"] is None:
                    found = list()
                    for experiment_name in experiments.keys():
                        experiment = Experiment(
                            bucket_name=S3_BUCKET,
                            elasticsearch_client=ELASTICSEARCH_CLIENT,
                            local_data_store=Path("static/data"),
                            name=experiment_name,
                            s3_client=S3_CLIENT,
                            debug=DEBUG,
                        )
                        try:
                            found.extend(
                                [
                                    {
                                        "doc": doc,
                                        "image_bytes": base64.b64encode(
                                            img_path.read_bytes()
                                        ).decode("utf-8"),
                                    }
                                    for doc, img_path in experiment.get(request["get"])
                                ]
                            )
                        except FileNotFoundError as e:
                            log.info(f"no match for get request '{e}'")
                else:
                    experiment = Experiment(
                        bucket_name=S3_BUCKET,
                        elasticsearch_client=ELASTICSEARCH_CLIENT,
                        local_data_store=Path("static/data"),
                        name=request["experiment"],
                        s3_client=S3_CLIENT,
                        debug=DEBUG,
                    )
                    try:
                        found = [
                            {
                                "doc": doc,
                                "image_bytes": base64.b64encode(
                                    img_path.read_bytes()
                                ).decode("utf-8"),
                            }
                            for doc, img_path in experiment.get(request["get"])
                        ]
                    except FileNotFoundError as e:
                        log.info(f"no match for get request '{e}'")
                        found = list()

                if len(found) > 0:
                    resp = {
                        "status": 200,
                        "found": found[0] if request["single_value"] else found
                    }
                else:
                    resp = {
                        "status": 404,
                        "message": "no colorgram for search term",
                        "query": request["get"],
                        "experiment": request["experiment"],
                    }
                log.info(f"sending JSON response through websocket with keys: {resp.keys()}")
                await websocket.send_json(resp)

        elif request["action"] == "list_experiments":
            await websocket.send_json(
                {"status": 200, "experiments": list(experiments.keys())}
            )
        elif request["action"] == "list_image_urls":
            image_urls = [
                image_url
                for image_url in get_response_value(
                    elasticsearch_client=ELASTICSEARCH_CLIENT,
                    index="raw-images",
                    query={
                        "query": {
                            "bool": {
                                "filter": request["filter"]
                            }
                        },
                        "aggregations": {
                            "image_url": {
                                "terms": {
                                    "field": "image_url",
                                    "size": 1000,
                                }
                            }
                        }
                    },
                    value_keys=["aggregations", "image_url", "buckets", "*", "key"],
                    size=0,
                    debug=True,
                )
            ]
            log.info(image_urls)
            await websocket.send_json(
                {"status": 200, "image_urls": image_urls}
            )



        else:
            await websocket.send_json(
                {"status": 404, "message": f"no action found for {request['action']}"}
            )


@app.route("/experiments/{experiment_name}")
@requires("authenticated", redirect="homepage")
async def experiment_csv(request: Request) -> JSONResponse:

    experiment_name = request.path_params["experiment_name"]

    try:
        response = await open_experiment_csv(
            get_experiment_csv_path(
                name=experiment_name, local_data_store=LOCAL_DATA_STORE,
            )
        )
        status_code = 200
    except FileNotFoundError as e:
        log.error(f"{experiment_name}: {e}")
        status_code = 404
        response = {
            "missing": experiment_name,
            "inventory": [
                csv.stem
                for csv in STATIC.joinpath("csv/experiments/").glob("*")
                if csv.suffix == ".csv"
            ],
        }
    except KeyError as e:
        log.error(f"{experiment_name}: {e}")
        status_code = 422
        response = {
            "invalid": experiment_name,
            "message": f"{experiment_name}.csv was found, but is missing a required column: {e}",
        }

    return JSONResponse(response, status_code=status_code)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()

    get_elasticsearch_args(parser)
    get_s3_args(parser)

    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    global ELASTICSEARCH_CLIENT
    global S3_BUCKET
    global S3_CLIENT
    global DEBUG
    ELASTICSEARCH_CLIENT, S3_CLIENT = get_clients(args)
    S3_BUCKET = args.s3_bucket
    DEBUG = args.debug

    uvicorn.run(app, host="0.0.0.0", port=8080, proxy_headers=True)
