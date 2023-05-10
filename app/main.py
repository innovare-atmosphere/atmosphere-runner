from fastapi.middleware.cors import CORSMiddleware
from fastapi import FastAPI, BackgroundTasks, Header, Body, Request
from fastapi.responses import RedirectResponse
from fastapi_mail import FastMail, MessageSchema, ConnectionConfig
from pydantic import BaseModel, BaseSettings
from typing import Optional
from string import Template
from random import randint
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
import random
import hashlib

logger = logging.getLogger(__name__)
subdomain_regex = re.compile("[0-9]*[a-z]+[a-z0-9]*")
email_regex = re.compile("^[a-z0-9]+[\._]?[a-z0-9]+[@]\w+[.]\w{2,3}$")

class PaymentStatus():
    invoked: str = "INVOKED"
    completed: str = "COMPLETED"

class TaskStatus():
    completed: str = "COMPLETED"
    destroyed: str = "DESTROYED"
    loading: str = "LOADING"
    deploy_error: str = "DEPLOY_ERROR"
    destroy_error: str = "DESTROY_ERROR"


class Settings(BaseSettings):
    frontend_url: str = "http://localhost:3000"
    db_url: str = "sqlite://storage.db"
    domain_root: str = "atmos.live"
    do_key: str = ""
    wait_value: int = 1
    nanoid_alphabet: str = "0123456789abcdefghijklmnopqrstuvwxyz-"
    nanoid_length: int = 21
    mail_username: str = ""
    mail_password: str = ""
    mail_from: str = ""
    mail_port: str = ""
    mail_server: str = ""
    mail_from_name: str = ""

    class Config:
        env_file = ".env"


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
running_destroy_containers = dict()


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
    # print(response.status_code)
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


def terra_destroy(id: str):
    output_destroy = ''
    error_status = False
    error = ''
    try:
        # Prepare task data
        task = db.task(uuid=id)
        output = task.output if task else None
        if not output:
            raise RuntimeError(
                "UUID ({}) Task has no output.".format(id)
            )
        if not output.get("status") in [TaskStatus.completed, TaskStatus.deploy_error, TaskStatus.destroy_error]:
            raise RuntimeError(
                "UUID ({}) Task is not completed.".format(id)
            )
        output["status"] = TaskStatus.loading
        task.update_record(output=output)
        db.commit()
        # Prepare terraform working directory
        local_path = '/tmp'  # workaround of docker /var/run/docker.sock os.getcwd()
        working_path = os.path.join(local_path, "workdir-{}".format(id))
        if not os.path.exists(working_path):
            raise RuntimeError(
                "UUID ({}) workdir doesn't exist. Aborting.".format(id)
            )
        repo_path = os.path.join(working_path, task.flavor)
        if not os.path.exists(repo_path):
            raise RuntimeError(
                "Couldn't find flavor ({}) for provider ({}). Aborting.".format(
                    task.flavor,
                    task.provider
                )
            )
        varsfile = 'atmosphere.tfvars.json'
        # Invoke terraform
        client = docker.from_env()
        stream_output_destroy = client.containers.run(
            "hashicorp/terraform:latest",
            "destroy -auto-approve -var-file=\"{}\"".format(varsfile),
            volumes={
                repo_path: {
                    'bind': '/workspace',
                    'mode': 'rw'
                }
            },
            detach=True,
            working_dir="/workspace"
        )
        running_destroy_containers[id] = stream_output_destroy
        o1 = stream_output_destroy.wait()
        output_destroy = stream_output_destroy.logs().decode("utf-8", "ignore")
        #{'Error': None, 'StatusCode': 0}
        if (o1['Error'] or o1['StatusCode'] != 0):
            raise RuntimeError(
                "Error found ({}) Status Code ({}).".format(
                    o1['Error'],
                    o1['StatusCode']
                )
            )
        # TODO: free the domain name
    except Exception as error_ex:
        error_status = True
        error = str(error_ex)
        logger.error(f"Task {id} errored.")
        logger.exception(error_ex)
    finally:
        # Catch output
        try:
            output_destroy = stream_output_destroy.logs().decode("utf-8", "ignore")
        except:
            pass
        # Cleanup
        try:
            stream_output_destroy.remove()
        except:
            pass
        try:
            del running_destroy_containers[id]
        except:
            pass
    # TODO: Pack everything and delete the working directory
    output["output_destroy"] = output_destroy
    output["error_destroy"] = error
    output["status"] = TaskStatus.destroy_error if error_status else TaskStatus.destroyed
    task.update_record(output=output)
    db.commit()
    return


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
                           status=TaskStatus.loading,
                           error=None
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
            output_done=output_done,
            error_deploy=error,
            status=TaskStatus.deploy_error if error_status else TaskStatus.completed
        )
    )
    db.commit()
    return


@app.get("/variables/{provider}/{flavor}")
def variables(provider: str, flavor: str, token: str = Header("")):
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
async def invoke(background_tasks: BackgroundTasks, provider: str, flavor: str, puuid: str, variables: dict, token: str = Header("")):
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
        #Generate the task uuid
        id = str(uuid.uuid4())
        #Payment processing using the puuid
        phistory = db((db.payment_history.token == token)&
           (db.payment_history.payment_validation_token == puuid)).select(
            db.payment_history.ALL,
            orderby=db.payment_history.id,
            limitby=(0, 1),
            distinct=True
           )
        if not phistory:
            raise RuntimeError(
                Template("Couldn't process payment id ($puuid) with current token ($token). Aborting.").substitute(
                    puuid = puuid,
                    token = token)
            )
        payment_history = phistory.first()
        payment_history.update_record(
            status = PaymentStatus.completed,
            what = id
        )
        org = db.organization(id = payment_history.organization)
        db((db.organization.id == pay_info.organization)).update(
            balance = (org.balance if org.balance is not None else 0) + payment_history.amount
        )
        db.commit()
        background_tasks.add_task(
            terra_invoke, id, token, provider, flavor, variables)
    except Exception as error_ex:
        error_status = True
        error = str(error_ex)
        logger.exception(error_ex)
    return {"uuid": id, "error_status": error_status, "error": error}


@app.post("/destroy/{task_uuid}")
async def destroy(background_tasks: BackgroundTasks, task_uuid: str, token: str = Header("")):
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
        background_tasks.add_task(
            terra_destroy, task_uuid)
    except Exception as error_ex:
        error_status = True
        error = str(error_ex)
        logger.exception(error_ex)
    return {"uuid": task_uuid, "error_status": error_status, "error": error}


@app.get("/state/{task_uuid}")
def task_state(task_uuid: str, token: str = Header("")):
    error_status = False
    error = None
    output = None
    try:
        if not valid_token(token):
            raise RuntimeError(
                "Token {} is not valid.".format(
                    token
                )
            )
        # TODO: Validate when token is valid but requested task doesnt belong to token/organization
        task_info = db.task(uuid=task_uuid)
        output = task_info.output if task_info else None
        print(output.get("status"))
        # print(type(status))
        if output and output.get("status") == TaskStatus.loading:
            if running_init_containers.get(task_uuid):
                output["output_init"] = running_init_containers.get(
                    task_uuid).logs()
            if running_apply_containers.get(task_uuid):
                output["output_apply"] = running_apply_containers.get(
                    task_uuid).logs()
            if running_destroy_containers.get(task_uuid):
                output["output_destroy"] = running_destroy_containers.get(
                    task_uuid).logs()
    except Exception as error_ex:
        error_status = True
        error = str(error_ex)
        logger.exception(error_ex)
    return {"uuid": task_uuid, "output": output, "error_status": error_status, "error": error}


@app.get("/my-account")
def my_account(token: str = Header("")):
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
        distinct = db.task.id
        # bugfix when using sqlite
        if 'sqlite' in settings.db_url:
            distinct = True
        user = db((db.access_token.token == token) & (
            db.access_token.owner == db.user.id))._select(db.user.id, distinct=True)
        user_tokens = db(db.access_token.owner.belongs(user)
                         )._select(db.access_token.token, distinct=True)
        all_tasks = db((db.task.organization == db.organization.id) &
                       (db.organization.id == db.user_organization.organization) &
                       (db.user_organization.user == db.access_token.owner) &
                       (db.access_token.token.belongs(user_tokens))).select(db.task.ALL, db.organization.ALL, orderby=~db.task.id, distinct=distinct).as_list()
        user_data = db.user(db.user.id.belongs(user)).as_dict()
        user_data["md5"] = hashlib.md5(user_data["email"].encode()).hexdigest()
        del user_data['id']
        del user_data['password_hash']
        user_organizations = db((db.user_organization.user.belongs(user)) &
                                (db.organization.id == db.user_organization.organization)).select(db.organization.ALL).as_list()
    except Exception as error_ex:
        error_status = True
        error = str(error_ex)
        logger.exception(error_ex)
    return {"all_tasks": all_tasks, "error": error, "error_status": error_status, "user": user_data, "organizations": user_organizations}


class PaymentInformation(BaseModel):
    card_number: Optional[str] = None
    cardholder: Optional[str] = None
    cvv: Optional[str] = None
    exp_date: Optional[str] = None
    type: str


class PaymentDetails(BaseModel):
    method: str
    provider: str
    flavor: str
    information: Optional[PaymentInformation] = None


@app.post("/pay")
def my_tasks(details: PaymentDetails = Body(None, embed=True), token: str = Header("")):
    error_status = False
    error = None
    success = False
    puuid = None
    try:
        if not valid_token(token):
            raise RuntimeError(
                "Token {} is not valid.".format(
                    token
                )
            )
        organization = ''
        found_organization = db(
            (db.access_token.token == token) &
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
        if details.information.type == 'balance':
            success = True
            puuid = str(uuid.uuid4())
            #TODO: get the total to charge the user
            total = 10
            payment_id = db.payment_history.insert(
                token=token,
                payment_validation_token = puuid,
                amount=-total,
                status=PaymentStatus.invoked,
                organization=organization.id
            )
        if details.information.type == "24hours":
            success = True  # random.choice([True, False])
            puuid = str(uuid.uuid4())
            #TODO: get the total to charge the user
            total = 10
            payment_id = db.payment_history.insert(
                token=token,
                payment_validation_token = puuid,
                amount=-total,
                status=PaymentStatus.invoked,
                organization=organization.id
            )
    except Exception as error_ex:
        error_status = True
        error = str(error_ex)
        logger.exception(error_ex)
    return {"success": success, "error": error, "error_status": error_status, "puuid": puuid}


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
        db.access_token.insert(
            token=id, owner=None, valid_since=None, created_at=datetime.datetime.now())
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
async def validate(email: str = Body(None, embed=True), validation: Validation = Body(None, embed=True)):
    error_status = False
    error = ""
    valid_item = False
    valid_email = False
    try:
        # validate email
        valid_email = re.search(email_regex, email) is not None
        if not valid_email:
            raise RuntimeError(
                "Email {} is not valid.".format(
                    email
                )
            )
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
            # Create user (or find it)
            the_user = db.user(email=email)
            if not the_user:
                the_user = db.user.insert(email=email, password_hash=None)
                # Create default organization and random subdomain
                new_org = db.organization.insert(
                    name='My organization', subdomain=nanoid.generate(settings.nanoid_alphabet, settings.nanoid_length))
                # Relate user and organization
                db.user_organization.insert(
                    user=the_user, organization=new_org)
            # Send e-mail with code
            code = randint(100000, 999999)
            message = MessageSchema(
                subject="Login code to Atmosphere",
                recipients=[email],
                template_body=dict(body=dict(email=email, code=str(code))),
                subtype='html',
            )
            # print(settings)
            fm = FastMail(ConnectionConfig(
                MAIL_USERNAME=settings.mail_username,
                MAIL_PASSWORD=settings.mail_password,
                MAIL_FROM=settings.mail_from,
                MAIL_PORT=settings.mail_port,
                MAIL_SERVER=settings.mail_server,
                MAIL_FROM_NAME=settings.mail_from_name,
                MAIL_TLS=False,
                MAIL_SSL=True,
                USE_CREDENTIALS=True,
                TEMPLATE_FOLDER='./templates/email'
            ))
            await fm.send_message(message, template_name='email.html')
            # Update access_token
            item.update_record(owner=the_user, two_step=code)
            db.commit()
    except Exception as error_ex:
        error_status = True
        error = str(error_ex)
        logger.exception(error_ex)
    return {
        "valid": valid_item,
        "valid_email": valid_email,
        "token": validation.token,
        "error": error,
        "error_status": error_status
    }


@app.post("/validate2")
def validate2(code: str = Body(None, embed=True), token: str = Header("")):
    error_status = False
    error = ""
    valid = False
    try:
        # retrieve token
        item = db.access_token(token=token)
        if not item:
            raise RuntimeError(
                "Token {} is not valid.".format(
                    token
                )
            )
        # TODO: Case where user has attempted too much or the token has expired
        # validate code
        if int(item.two_step) != int(code):
            raise RuntimeError(
                "Invalid validation code {} and {}".format(
                    item.two_step,
                    code
                )
            )
        # Update access_token
        item.update_record(valid_since=datetime.datetime.now())
        db.commit()
        valid = True
    except Exception as error_ex:
        error_status = True
        error = str(error_ex)
        logger.exception(error_ex)
    return {
        "token": token,
        "valid": valid,
        "error": error,
        "error_status": error_status
    }


@app.get("/flavors/{provider_name}")
def flavor_data(provider_name: str):
    flavor_details = db(
        (db.provider.name == provider_name) &
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
                flavor=detail.flavor.name,
                installation=dict(
                    normal=detail.flavor.installation_pricing_normal,
                    discounted=detail.flavor.installation_pricing_discounted
                ),
                monthly=detail.flavor.monthly,
                payment_required=detail.flavor.payment_required or False
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

@app.post("/webhook-payment/")
async def webhook_payment(request: Request):
    error_status = False
    error = None
    try:
        event = await request.json()
        event_status = event.get("resource").get("status")
        if not(event_status == "COMPLETED"):
            raise RuntimeError(
                Template(
                    "Ignoring webhook payment {$ern} and token {$token}, because event status is {$status}.").substitute(
                    ern = event.get("resource").get("ern"),
                    token = event.get("resource").get("token"),
                    status = event_status
                )
            )
        r = valid_payment(
            event.get("resource").get("token"),
            event.get("resource").get("ern"),
            True)
        return r
    except Exception as error_ex:
        error_status = True
        error = str(error_ex)
        logger.exception(error_ex)
    return {
        "error_status": error_status,
        "error": error
    }

@app.get("/valid_payment/{pay_token}/{ern}")
def valid_payment(pay_token: str, ern: str, no_redirect: bool = False):
    error_status = False
    error = None
    try:
        #Get the payment
        check_payment = db(
            (db.payment_history.payment_validation_token == pay_token) &
            (db.payment_history.id == ern)
        ).select(
            db.payment_history.ALL,
            orderby=db.payment_history.id,
            limitby=(0, 1),
            distinct=True
        )
        if not check_payment:
            raise RuntimeError(
                    Template(
                        "Payment identified by ern ($ern) has no token {$token} or status is not {$status}. Failed validation.").substitute(
                        ern = ern,
                        token = pay_token,
                        status = PaymentStatus.invoked
                    )
                )
        pay_info = check_payment.first()
        #Ignore when payment already applied
        if pay_info.status == PaymentStatus.completed:
            if no_redirect:
                return {
                    "error_status": error_status,
                    "error": error
                }
            return RedirectResponse("{}/my-account".format(
                settings.frontend_url
            ))
        amount = pay_info.amount
        #Update the payment as valid
        db.payment_history.update_or_insert(
            ((db.payment_history.payment_validation_token == pay_token) &
            (db.payment_history.id == ern) &
            (db.payment_history.status == PaymentStatus.invoked)),
            status = PaymentStatus.completed
        )
        #Update the balance
        org = db.organization(id = pay_info.organization)
        db((db.organization.id == pay_info.organization)).update(
            balance = (org.balance if org.balance is not None else 0) + amount
        )
        #Commit changes
        db.commit()
        #redirect user
        if no_redirect:
            return {
                "error_status": error_status,
                "error": error
            }
        return RedirectResponse("{}/my-account".format(
            settings.frontend_url
        ))
    except Exception as error_ex:
        error_status = True
        error = str(error_ex)
        logger.exception(error_ex)
    return {
        "error_status": error_status,
        "error": error
    }


@app.get("/payment/{total}")
def payment(total: float, token: str = Header("")):
    error_status = False
    error = ""
    redirect_url = ""
    try:
        if not total>0:
            raise RuntimeError(
                "Total to pay must be positive, received: {total}.".format(
                    token
                )
            )
        if not valid_token(token):
            raise RuntimeError(
                "Token {} is not valid.".format(
                    token
                )
            )
        organization = ''
        found_organization = db(
            (db.access_token.token == token) &
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
        db.commit()
        #Call on connect (get the pagadito token)
        import http.client
        import urllib.parse
        conn = http.client.HTTPSConnection("sandbox.pagadito.com")
        params = dict(
            operation = "f3f191ce3326905ff4403bb05b0de150",
            uid = "8e8712534becd984184b447497b6aed1",
            wsk = "e367e770853f3887e6dd99b7ea625a0b",
            format_return = "json"
        )
        payload = urllib.parse.urlencode(params)
        headers = { 'content-type': "application/x-www-form-urlencoded" }
        conn.request("POST", "/comercios/apipg/charges.php", payload, headers)
        res = conn.getresponse()
        data = res.read()
        jdata = json.loads(data)
        if not jdata.get("code") == "PG1001":
            raise RuntimeError(
                "Calling Pagadito API connect error: {} message: {}.".format(
                    jdata.get("code"),
                    jdata.get("message")
                )
            )
        pagadito_token = jdata.get("value")
        #Create a "invoked" payment
        payment_id = db.payment_history.insert(
            token=token,
            payment_validation_token = pagadito_token,
            amount=total,
            status=PaymentStatus.invoked,
            organization=organization.id
        )
        #After that, call to create the url
        conn = http.client.HTTPSConnection("sandbox.pagadito.com")
        params = dict(
            operation="41216f8caf94aaa598db137e36d4673e",
            token=pagadito_token,
            ern=payment_id,
            amount=total,
            details=json.dumps(
                [
                dict(
                    quantity = 1,
                    description = "Atmosphere credits for US{}".format(total),
                    price = total,
                    url_product = "",
                )
                ]
            ),
            format_return = "json",
            currency = "USD"
        )
        payload = urllib.parse.urlencode(params)
        headers = { 'content-type': "application/x-www-form-urlencoded" }
        conn.request("POST", "/comercios/apipg/charges.php", payload, headers)
        res = conn.getresponse()
        data = res.read()
        jdata2 = json.loads(data)
        if not jdata2.get("code") == "PG1002":
            raise RuntimeError(
                "Error Calling Pagadito API create url payment error: {} message: {}.".format(
                    jdata2.get("code"),
                    jdata2.get("message")
                )
            )
        redirect_url = jdata2.get("value")
        #Capture the url
    except Exception as error_ex:
        error_status = True
        error = str(error_ex)
        logger.exception(error_ex)
    return {
        "redirect_url": redirect_url,
        "error": error,
        "error_status": error_status
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


@app.get("/migrate")
async def migrate():
    all_tasks = db(db.task.id > 0).select()
    for task in all_tasks:
        if (type(task.output) == type(dict())):
            if task.output.get("error"):
                task.output["error_deploy"] = task.output.get("error")
                del task.output["error"]
            if task.output.get("error_status") == True:
                del task.output["error_status"]
                del task.output["completed"]
                task.output["status"] = TaskStatus.deploy_error
            elif task.output.get("completed") == True:
                del task.output["error_status"]
                del task.output["completed"]
                task.output["status"] = TaskStatus.completed
            elif task.output.get("completed") == False:
                del task.output["error_status"]
                del task.output["completed"]
                task.output["status"] = TaskStatus.loading
            task.update_record(output=task.output)
    db.commit()
    return {
        "finished": True,
        "error": "",
        "error_status": False
    }
