from fastapi.middleware.cors import CORSMiddleware
from fastapi import FastAPI, BackgroundTasks, Header, Body
from pydantic import BaseModel, BaseSettings
from typing import Optional
import logging
import docker
import os
import uuid
import json
import git
import shutil
import random
import base64
import string
from captcha.image import ImageCaptcha

logger = logging.getLogger(__name__)

class Settings(BaseSettings):
    frontend_url: str = "http://localhost:3000"


settings = Settings()
app = FastAPI()

origins = [
    settings.frontend_url,
    "http://127.0.0.1:3000",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# TODO: this should be replaced by some backend store like redis
storage = dict()
tokens = dict()


def prefill_data(id: str, stream, key: str):
    obj = storage.get(id)
    if not obj:
        obj = dict()
        storage[id] = obj
    for x in stream:
        oldval = obj.get(key) or ''
        obj[key] = oldval + str(x, 'utf-8')
        storage[id] = obj
    return obj[key]


def valid_token(token: str):
    item = tokens.get(token)
    if not item:
        return False
    return item.get("valid") == True


def terra_invoke(id: str, provider: str, flavor: str, variables: dict):
    output_init = ''
    output_apply = ''
    error_status = False
    error = ''
    try:
        # Prepare terraform working directory
        local_path = os.getcwd()
        working_path = os.path.join(local_path, "workdir-{}".format(id))
        if os.path.exists(working_path):
            raise RuntimeError(
                "UUID ({}) workdir already exists. Aborting.".format(id)
            )
        os.mkdir(working_path)
        # Pull the appropiate data source
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
        # Create variables file in working directory
        varsfile = 'atmosphere.tfvars.json'
        varspath = os.path.join(repo_path, varsfile)
        with open(varspath, 'w') as json_file:
            json.dump(variables, json_file)
        # Invoke terraform
        client = docker.from_env()
        stream_output_init = client.containers.run(
            "hashicorp/terraform:latest",
            "init -input=false",
            volumes={
                repo_path: {
                    'bind': '/workspace',
                    'mode': 'rw'
                }
            },
            stream=True,
            working_dir="/workspace"
        )
        #logger.warning('this should be first')
        output_init = prefill_data(id, stream_output_init, "output_init")
        # logger.warning(output_init)
        #logger.warning('this should be next')
        #output_init = str(output_init, "utf-8")
        stream_output_apply = client.containers.run(
            "hashicorp/terraform:latest",
            "apply -auto-approve -json -var-file=\"{}\"".format(varsfile),
            volumes={
                repo_path: {
                    'bind': '/workspace',
                    'mode': 'rw'
                }
            },
            stream=True,
            working_dir="/workspace"
        )
        output_apply = prefill_data(id, stream_output_apply, "output_apply")
        #output_apply = str(output_apply, "utf-8")
    except Exception as error_ex:
        error_status = True
        error = str(error_ex)
        logger.exception(error_ex)
    # TODO: Pack everything and delete the working directory
    storage[id] = dict(
        output_init=output_init,
        output_apply=output_apply,
        error_status=error_status,
        error=error,
        completed=True
    )
    return


@app.get("/variables/{provider}/{flavor}")
def variables(provider: str, flavor: str, token: str = Header("")):
    error_status = False
    error = None
    output = None
    id = str(uuid.uuid4())
    local_path = os.getcwd()
    working_path = os.path.join(local_path, "workdir-{}".format(id))
    try:
        if not valid_token(token):
            raise RuntimeError(
                "Token {} is not valid.".format(
                    token
                )
            )
        # Prepare terraform working directory
        if os.path.exists(working_path):
            raise RuntimeError(
                "UUID ({}) workdir already exists. Aborting.".format(id)
            )
        os.mkdir(working_path)
        # Pull the appropiate data source
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
            volumes={
                repo_path: {
                    'bind': '/workspace',
                    'mode': 'rw'
                }
            },
            working_dir="/workspace"
        )
        output = json.loads(output)
    except Exception as error_ex:
        error_status = True
        error = str(error_ex)
        logger.exception(error_ex)
    finally:
        shutil.rmtree(working_path)
    return dict(
        output=output,
        error_status=error_status,
        error=error
    )


@app.post("/invoke/{provider}/{flavor}")
async def invoke(background_tasks: BackgroundTasks, provider: str, flavor: str, variables: dict, token: str = Header("")):
    error_status = False
    error = None
    id = None
    try:
        if not valid_token(token):
            raise RuntimeError(
                "Token {} is not valid.".format(
                    token
                )
            )
        id = str(uuid.uuid4())
        background_tasks.add_task(
            terra_invoke, id, provider, flavor, variables)
    except Exception as error_ex:
        error_status = True
        error = str(error_ex)
        logger.exception(error_ex)
    return {"uuid": id, "error_status": error_status, "error": error}


@app.get("/state/{task_uuid}")
def task_state(task_uuid: str, token: str = Header("")):
    error_status = False
    error = None
    status = None
    try:
        if not valid_token(token):
            raise RuntimeError(
                "Token {} is not valid.".format(
                    token
                )
            )
        status = storage.get(task_uuid)
    except Exception as error_ex:
        error_status = True
        error = str(error_ex)
        logger.exception(error_ex)
    return {"uuid": task_uuid, "status": status, "error_status": error_status, "error": error}


@app.get("/token")
def authorize():
    error_status = False
    error = None
    captcha_image = None
    id = None
    try:
        image = ImageCaptcha(width=200, height=60, font_sizes=[45])
        captcha_size = 5
        text_validation = "".join([random.choice(string.ascii_lowercase)
                                   for i in range(captcha_size)])
        # TODO: store in local memory (should be REDIS and should expire)
        id = str(uuid.uuid4())
        tokens[id] = dict(text_validation=text_validation, valid=False)
        bytes_png = image.generate(text_validation)
        captcha_image = base64.b64encode(bytes_png.read())
    except Exception as error_ex:
        error_status = True
        error = str(error_ex)
        logger.exception(error_ex)
    return {"captcha": captcha_image, "token": id, "error": error, "error_status": error_status}

class Validation(BaseModel):
    token: str
    proof: str

@app.post("/validate")
def validate(validation:Validation = Body(None, embed = True)):
    error_status = False
    error = ""
    valid_item = False
    try:
        item = tokens.get(validation.token)
        if item is None:
            raise RuntimeError(
                "Invalid token {}".format(
                    validation.token
                )
            )
        item["valid"] = (item.get("text_validation") == validation.proof)
        valid_item = item["valid"]
        tokens[validation.token] = item
    except Exception as error_ex:
        error_status = True
        error = str(error_ex)
        logger.exception(error_ex)
    return {
        "valid": valid_item,
        "token": validation.token,
        "error": error,
        "error_status": error_status
    }
