import logging
import docker
import os
import uuid
import json
import git
import shutil

logger = logging.getLogger(__name__)

from typing import Optional

from fastapi import FastAPI, BackgroundTasks

from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

origins = [
    "http://localhost:3000",
    "https://localhost:3000",
    "http://127.0.0.1:3000",
    "https://127.0.0.1:3080",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
#TODO: this should be replaced by some backend store like redis
storage = dict()

def prefill_data(id: str, stream, key:str):
    obj = storage.get(id)
    if not obj:
        obj = dict()
        storage[id] = obj
    for x in stream:
        oldval = obj.get(key) or ''
        obj[key] = oldval + str(x, 'utf-8')
        storage[id] = obj
    return obj[key]


def terra_invoke(id:str, provider: str, flavor: str, variables: dict):
    output_init = ''
    output_apply = ''
    error_status = False
    error = ''
    try:
        #Prepare terraform working directory
        local_path = os.getcwd()
        working_path = os.path.join(local_path, "workdir-{}".format(id))
        if os.path.exists(working_path):
            raise RuntimeError(
                "UUID ({}) workdir already exists. Aborting.".format(id)
            )
        os.mkdir(working_path)
        #Pull the appropiate data source
        repo = git.Repo.clone_from(
            "https://github.com/innovare-atmosphere/{}".format(provider),
            working_path,
            branch='main'
        )
        repo_path = os.path.join(working_path, flavor)
        if not os.path.exists(repo_path):
            raise RuntimeError(
                "Couldn't find flavor ({}) for provider ({}). Aborting.".format(
                    flavor, 
                    provider
                )
            )
        #Create variables file in working directory
        varsfile = 'atmosphere.tfvars.json'
        varspath = os.path.join(repo_path, varsfile)
        with open(varspath, 'w') as json_file:
            json.dump(variables, json_file)
        #Invoke terraform
        client = docker.from_env()
        stream_output_init = client.containers.run(
            "hashicorp/terraform:latest",
            "init -input=false",
            volumes = {
                repo_path: {
                    'bind': '/workspace',
                    'mode': 'rw'
                }
            },
            stream = True,
            working_dir = "/workspace"
        )
        #logger.warning('this should be first')
        output_init = prefill_data(id, stream_output_init, "output_init")
        #logger.warning(output_init)
        #logger.warning('this should be next')
        #output_init = str(output_init, "utf-8")
        stream_output_apply = client.containers.run(
            "hashicorp/terraform:latest",
            "apply -auto-approve -json -var-file=\"{}\"".format(varsfile),
            volumes = {
                repo_path: {
                    'bind': '/workspace',
                    'mode': 'rw'
                }
            },
            stream = True,
            working_dir = "/workspace"
        )
        output_apply = prefill_data(id, stream_output_apply, "output_apply")
        #output_apply = str(output_apply, "utf-8")
    except Exception as error_ex:
        error_status = True
        error = str(error_ex)
        logger.exception(error_ex)
    #TODO: Pack everything and delete the working directory
    storage[id] = dict(
        output_init = output_init,
        output_apply = output_apply,
        error_status = error_status,
        error = error,
        completed = True
    )
    return

@app.get("/variables/{provider}/{flavor}")
def variables(provider: str, flavor: str):
    error_status = False
    error = None
    output = None
    try:
        id = str(uuid.uuid4())
        #Prepare terraform working directory
        local_path = os.getcwd()
        working_path = os.path.join(local_path, "workdir-{}".format(id))
        if os.path.exists(working_path):
            raise RuntimeError(
                "UUID ({}) workdir already exists. Aborting.".format(id)
            )
        os.mkdir(working_path)
        #Pull the appropiate data source
        repo = git.Repo.clone_from(
            "https://:@github.com/innovare-atmosphere/{}".format(provider),
            working_path,
            branch='main'
        )
        repo_path = os.path.join(working_path, flavor)
        if not os.path.exists(repo_path):
            raise RuntimeError(
                "Couldn't find flavor ({}) for provider ({}). Aborting.".format(
                    flavor, 
                    provider
                )
            )
        client = docker.from_env()
        output = client.containers.run(
            "claranet/terraform-ci",
            "terraform-config-inspect --json ./",
            volumes = {
                repo_path: {
                    'bind': '/workspace',
                    'mode': 'rw'
                }
            },
            working_dir = "/workspace"
        )
        shutil.rmtree(working_path)
        output = json.loads(output)
    except Exception as error_ex:
        error_status = True
        error = str(error_ex)
        logger.exception(error_ex)
    return dict(
        output = output,
        error_status = error_status,
        error = error
    )

@app.post("/invoke/{provider}/{flavor}")
async def invoke(background_tasks: BackgroundTasks, provider: str, flavor: str, variables: dict):
    id = str(uuid.uuid4())
    background_tasks.add_task(terra_invoke, id, provider, flavor, variables)
    return {"uuid": id}


@app.get("/state/{task_uuid}")
def task_state(task_uuid: str):
    return {"uuid": task_uuid, "status": storage.get(task_uuid)}
