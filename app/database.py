from pydal import DAL, Field
import datetime

def Database(uri):
    db = DAL(uri)
    db.define_table(
        'user',
        Field('email', 'string', distinct = True),
        Field('password_hash', 'string')
    )

    db.define_table(
        'access_token',
        Field('owner', 'reference user'),
        Field('token', 'string', distinct = True),
        Field('valid_since', 'datetime'),
        Field('created_at', 'datetime', default = datetime.datetime.now()),
    )

    db.define_table(
        'organization',
        Field('name', 'string'),
        Field('subdomain', 'string', distinct = True),
    )

    db.define_table(
        'user_organization',
        Field('user', 'references user'),
        Field('organization', 'references organization')
    )

    db.define_table(
        'task',
        Field('uuid', 'string'),
        Field('provider', 'string'),
        Field('flavor', 'string'),
        Field('domain', 'string'),
        Field('organization', 'references organization'),
        Field('created_at', 'datetime', default = datetime.datetime.now()),
        Field('output', 'text')
    )
    return db