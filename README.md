# imgserve

The goal of this program is to help automate the process of image collection and analysis.

## requirements

`poetry`: https://python-poetry.org/docs/
`docker`: https://docs.docker.com/get-docker/
`python3`: https://www.python.org/downloads/

## quickstart

NOTE: quickstart will only work on Unix systems like Mac OS and Debian, but this code should run on any system that can satisfy the requirements.

1. satisfy requirements
2. run `poetry run ./bin/init.py`
3. run `source .env`

NOTE: you must run `source .env` every time you come to this folder in a new shell, it sets up secret values in your command line environment.

to contribute results to a given <experiment_name>:

```
./experiment.sh --trial-ids ${IMGSERVE_HOSTNAME} --experiment-name null-test --run-trial
```

to look at colorgrams for all trials run from this host:

```
./experiment.sh --trial-ids ${IMGSERVE_HOSTNAME} --experiment-name null-test --dimensions query trial_timestamp
```

to look at colorgrams for all trials run from this host and any other host, tagging by `hostname` for richer colorgram annotation:

```
./experiment.sh --trial-ids ${IMGSERVE_HOSTNAME} <other-host-trial-id-1> <other-host-trial-id-2> --experiment-name null-test --dimensions query trial_timestamp trial_id hostname
```

These shell scripts are light wrappers around poetry calls, mostly to keep the number of arguments required to a minimum. They are meant to make the basic usage of this program very simple, but are not required.

## Experiment

This program facilitates the execution of "experiments". Each "experiment" is logically oriented around a particular hypothesis, and has a unique `experiment_name` associated with it. Each `experiment_name` has 1 or more `trial_id` values associated with it. 

To do a time trial experiment, one would run the experiment code with the `--run-trial` flag every interval, holding the `--trial-ids` and `--experiment-name` values constant. Each run can be differentiated by the `trial_timestamp` field, which gets added automatically by the trial runner code.

To contribute to a global search experiment, one would run the experiment code with the provided `--experiment-name` and `--trial-ids` value. Host-specific metadata is automatically associated with the contributed results.

The queries to run for each experiment are stored in csv format. By default, this program fetches experiment csvs from the shared remote url, using configuration values from the `.env` file. If you would like to develop your own experiments locally, you can do so by adding the option `--remote-url http://localhost:8080` to your calls to `experiment.py`, this will serve csvs locally, but you must start the imgserve server first:

## Running the imgserve server

This repository also includes a web application for viewing results in the browser, to run it:

`cd ./app && poetry run ./server.py`

This will start a listener at `localhost:8080`. Images and experiment csvs are served out of the `IMGSERVE_LOCAL_DATA_STORE` path configured in `.env`. To start developing a new experiment, create a .csv file at `${IMGSERVE_LOCAL_DATA_STORE}/imgserve/experiments/<experiment_name>.csv`. This file must have the following headers: `search_term`, `region`. Any additional columns will be associated with the raw data results.

open `localhost:8080` in your browser to view experiment results.
