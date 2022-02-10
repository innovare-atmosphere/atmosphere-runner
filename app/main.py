from fastapi.middleware.cors import CORSMiddleware
from fastapi import FastAPI, BackgroundTasks, Header, Body
from pydantic import BaseModel, BaseSettings
from typing import Optional
from string import Template
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
from .database import Database
import datetime
import nanoid
import re
import requests
import time

logger = logging.getLogger(__name__)
subdomain_regex = re.compile("[0-9]*[a-z]+[a-z0-9]*")


class Settings(BaseSettings):
    frontend_url: str = "http://localhost:3000"
    db_url: str = "sqlite://storage.db"
    domain_root: str = "atmos.live"
    do_key: str = ""
    wait_value: int = 1
    nanoid_alphabet: str = "0123456789abcdefghijklmnopqrstuvwxyz-"
    nanoid_length: int = 21


settings = Settings()
app = FastAPI()
db = Database(settings.db_url)

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
memory_tokens = dict()
running_init_containers = dict()
running_apply_containers = dict()


def attach_subdomain(ip_address, domain):
    url = f"https://api.digitalocean.com/v2/domains/{settings.domain_root}/records"
    subdomain = domain.replace(f".{settings.domain_root}", "")
    payload = dict(type="A", name=subdomain, data=ip_address, priority=None,
                   port=None, ttl="1800", weight=None, flags=None, tag=None)
    headers = {
        'content-type': "application/json",
        'authorization': f"Bearer {settings.do_key}",
        'cache-control': "no-cache"
    }
    response = requests.request(
        "POST", url, data=json.dumps(payload), headers=headers)
    #print(response.status_code)
    if not (response.status_code in [200, 201, 202, 203, 204, 205, 206]):
        raise RuntimeError(
            Template(f"Error attaching subdomain ($subdomain) for $domain to ip $ip_address. [{response.text}] Aborting.").substitute(
                domain=domain, subdomain=subdomain, ip_address=ip_address)
        )
    return


def wait_until_container_log(container, regex, action, *args, **kwargs):
    logger.info(f"Started to watch stream, waiting for {regex}")
    while True:
        log = container.logs().decode("utf-8", "ignore")
        attempt_match = re.search(regex, log)
        if attempt_match:
            reg_match_group = attempt_match.group(1)
            action(reg_match_group, *args, **kwargs)
            logger.info(f"Finished to watch stream, found {regex}")
            return log
        container.reload()
        if container.status != 'running':
            raise RuntimeError(
                Template("Execution finished before finding ($regex).").substitute(
                    regex=regex)
            )
        time.sleep(settings.wait_value)


def valid_token(token: str):
    item = db.access_token(token=token)
    if not item:
        return False
    return item.valid_since != None


def terra_invoke(id: str, user_token: str, provider: str, flavor: str, variables: dict):
    output_init = ''
    output_apply = ''
    output_done = ''
    error_status = False
    error = ''
    try:
        # Prepare task data
        organization = ''
        found_organization = db(
            (db.access_token.token == user_token) &
            (db.access_token.owner == db.user_organization.user) &
            (db.organization.id == db.user_organization.organization)
        ).select(
            db.organization.ALL,
            orderby=db.organization.id,
            limitby=(0, 1),
            distinct=True
        )
        if not found_organization:
            raise RuntimeError(
                Template("User identified by token ($user_token) has no organization. Aborting.").substitute(
                    user_token=user_token)
            )
        organization = found_organization.first()
        domain = variables.get('domain')
        subdomain = ''
        # or not valid domain
        if not domain or not subdomain_regex.fullmatch(domain):
            # Generate a new domain
            subdomain = nanoid.generate(
                settings.nanoid_alphabet, settings.nanoid_length)
        else:
            # Apply domain as a subdomain
            # TODO: use case where domain is a valid one and not a subdomain
            subdomain = domain
        variables['domain'] = Template(f"$subdomain.$organization.{settings.domain_root}").substitute(
            subdomain=subdomain, organization=organization.subdomain)
        # TODO: Validate subdomain to be available, else include a default
        db.task.insert(uuid=id,
                       provider=provider,
                       flavor=flavor,
                       domain=variables['domain'],
                       organization=organization.id,
                       output=dict(
                           output_init="",
                           output_apply="",
                           output_done="",
                           error_status=False,
                           error=None,
                           completed=False
                       ))
        db.commit()
        # Prepare terraform working directory
        local_path = '/tmp'  # workaround of docker /var/run/docker.sock os.getcwd()
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
            detach=True,
            working_dir="/workspace"
        )
        running_init_containers[id] = stream_output_init
        o1 = stream_output_init.wait()
        output_init = stream_output_init.logs().decode("utf-8", "ignore")
        #{'Error': None, 'StatusCode': 0}
        if (o1['Error'] or o1['StatusCode'] != 0):
            raise RuntimeError(
                "Error found ({}) Status Code ({}).".format(
                    o1['Error'],
                    o1['StatusCode']
                )
            )
        #logger.warning('this should be first')
        #output_init = prefill_data(id, stream_output_init, "output_init")
        # logger.warning(output_init)
        #logger.warning('this should be next')
        #output_init = str(output_init, "utf-8")
        stream_output_apply = client.containers.run(
            "hashicorp/terraform:latest",
            "apply -auto-approve -var-file=\"{}\"".format(varsfile),
            volumes={
                repo_path: {
                    'bind': '/workspace',
                    'mode': 'rw'
                }
            },
            detach=True,
            working_dir="/workspace"
        )
        running_apply_containers[id] = stream_output_apply
        wait_until_container_log(
            stream_output_apply,
            regex="Host: (.*)\n",
            action=attach_subdomain,
            domain=variables['domain'])
        o2 = stream_output_apply.wait()
        output_apply = stream_output_apply.logs().decode("utf-8", "ignore")
        #{'Error': None, 'StatusCode': 0}
        if (o2['Error'] or o2['StatusCode'] != 0):
            raise RuntimeError(
                "Error found ({}) Status Code ({}).".format(
                    o2['Error'],
                    o2['StatusCode']
                )
            )
        #output_apply = prefill_data(id, stream_output_apply, "output_apply")
        #output_apply = str(output_apply, "utf-8")
        output_done = client.containers.run(
            "hashicorp/terraform:latest",
            "output --json",
            volumes={
                repo_path: {
                    'bind': '/workspace',
                    'mode': 'rw'
                }
            },
            working_dir="/workspace"
        )
        output_done = json.loads(output_done)
    except Exception as error_ex:
        error_status = True
        error = str(error_ex)
        logger.error(f"Task {id} errored.")
        logger.exception(error_ex)
    finally:
        # Catch output
        try:
            output_init = stream_output_init.logs().decode("utf-8", "ignore")
        except:
            pass
        try:
            output_apply = stream_output_apply.logs().decode("utf-8", "ignore")
        except:
            pass
        # Cleanup
        try:
            stream_output_init.remove()
        except:
            pass
        try:
            del running_init_containers[id]
        except:
            pass
        try:
            stream_output_apply.remove()
        except:
            pass
        try:
            del running_apply_containers[id]
        except:
            pass
    # TODO: Pack everything and delete the working directory
    db.task.update_or_insert(
        (db.task.uuid == id),
        uuid=id,
        output=dict(
            output_init=output_init,
            output_apply=output_apply,
            error_status=error_status,
            output_done=output_done,
            error=error,
            completed=True
        )
    )
    db.commit()
    return


@app.get("/variables/{provider}/{flavor}")
def variables(provider: str, flavor: str, payment: dict = dict(), token: str = Header("")):
    error_status = False
    error = None
    output = None
    id = str(uuid.uuid4())
    local_path = '/tmp'  # workaround of docker /var/run/docker.sock os.getcwd()
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
        try:
            shutil.rmtree(working_path)
        except:
            pass
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
            terra_invoke, id, token, provider, flavor, variables)
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
    reported_ip_address = None
    try:
        if not valid_token(token):
            raise RuntimeError(
                "Token {} is not valid.".format(
                    token
                )
            )
        # TODO: Validate when token is valid but requested task doesnt belong to token/organization
        task_info = db.task(uuid=task_uuid)
        status = task_info.output if task_info else None
        #print(type(status))
        if status and not status["completed"]:
            if running_init_containers.get(task_uuid):
                status["output_init"] = running_init_containers.get(
                    task_uuid).logs()
            if running_apply_containers.get(task_uuid):
                status["output_apply"] = running_apply_containers.get(
                    task_uuid).logs()
    except Exception as error_ex:
        error_status = True
        error = str(error_ex)
        logger.exception(error_ex)
    return {"uuid": task_uuid, "status": status, "error_status": error_status, "error": error, "reported_ip_address": reported_ip_address}


@app.get("/my-tasks")
def my_tasks(token: str = Header("")):
    error_status = False
    error = None
    all_tasks = None
    try:
        if not valid_token(token):
            raise RuntimeError(
                "Token {} is not valid.".format(
                    token
                )
            )
        user = db((db.access_token.token == token) & (
            db.access_token.owner == db.user.id))._select(db.user.id)
        user_tokens = db(db.access_token.owner.belongs(user)
                         )._select(db.access_token.token)
        all_tasks = db((db.task.organization == db.organization.id) &
                       (db.organization.id == db.user_organization.organization) &
                       (db.user_organization.user == db.access_token.owner) &
                       (db.access_token.token.belongs(user_tokens))).select(db.task.ALL, orderby=~db.task.id).as_list()
    except Exception as error_ex:
        error_status = True
        error = str(error_ex)
        logger.exception(error_ex)
    return {"all_tasks": all_tasks, "error": error, "error_status": error_status}


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
        db.access_token.insert(token=id, owner=None, valid_since=None)
        db.commit()
        memory_tokens[id] = dict(text_validation=text_validation, valid=False)
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
def validate(validation: Validation = Body(None, embed=True)):
    error_status = False
    error = ""
    valid_item = False
    try:
        item = db.access_token(token=validation.token)
        memory_token = memory_tokens.get(validation.token)
        # TODO: Case where user has attempted too much or the token has expired
        if item is None or memory_token is None:
            raise RuntimeError(
                "Invalid token {}".format(
                    validation.token
                )
            )
        valid_item = (memory_token.get("text_validation") == validation.proof)
        if valid_item:
            # Create user
            new_user = db.user.insert(email=None, password_hash=None)
            # Update access_token
            item.update_record(
                valid_since=datetime.datetime.now(), owner=new_user)
            # Create default organization and random subdomain
            new_org = db.organization.insert(
                name='My organization', subdomain=nanoid.generate(settings.nanoid_alphabet, settings.nanoid_length))
            # Relate user and organization
            db.user_organization.insert(user=new_user, organization=new_org)
            db.commit()
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

@app.get("/flavors/{provider_name}")
def flavor_data(provider_name: str):
    flavor_details = db(
        (db.provider.name == provider_name)&
        (db.flavor.provider == db.provider.id)
    ).select()
    if not flavor_details:
        return {
            "error": "Flavors for provider {} not found".format(
                provider_name
            ),
            "error_status": True
        }
    pricing = []
    for detail in flavor_details:
        pricing.append(
            dict(
                flavor = detail.flavor.name,
                installation = dict(
                    normal = detail.flavor.installation_pricing_normal,
                    discounted = detail.flavor.installation_pricing_discounted
                ),
                monthly = detail.flavor.monthly,
                payment_required = detail.flavor.payment_required or False
            )
        )
    return {
        "display_name": flavor_details.first().provider.display_name,
        "name": flavor_details.first().provider.name,
        "description": flavor_details.first().provider.description,
        "url": flavor_details.first().provider.url,
        "logo": flavor_details.first().provider.logo,
        "version": flavor_details.first().provider.version,
        "pricing": pricing,
        "error_status": False
    }
    
@app.get("/providers")
def providers_list():
    providers = db(
        (db.provider.id >= 0)    
    ).select()
    if not providers:
        providers = []
    providers_output = []
    for provider in providers:
        providers_output.append(
            {
                "display_name": provider.display_name,
                "name": provider.name,
                "url": provider.url,
                "logo": provider.logo,
                "version": provider.version,
                "description": provider.description,
            }
        )
    return {
        "providers": providers_output,
        "error_status": False
    }