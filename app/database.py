from pydal import DAL, Field
import datetime

def Database(uri):
    db = DAL(uri, folder="./app/migrations")
    
    db.define_table(
        'provider',
        Field('name', 'string', distinct = True),
        Field('display_name', 'string'),
        Field('url', 'string'),
        Field('logo', 'string'),
        Field('version', 'string'),
        Field('description', 'text'),
    )
    
    db.define_table(
        'flavor',
        Field('name', 'string', distinct = True),
        Field('installation_pricing_normal', 'string'),
        Field('installation_pricing_discounted', 'string'),
        Field('monthly', 'string'),
        Field('provider', 'reference provider'),
        Field('payment_required', 'boolean')
    )
    
    db.define_table(
        'user',
        Field('email', 'string', distinct = True),
        Field('password_hash', 'string')
    )

    db.define_table(
        'access_token',
        Field('owner', 'reference user'),
        Field('token', 'string', distinct = True),
        Field('two_step', 'string'),
        Field('valid_since', 'datetime'),
        Field('created_at', 'datetime', default = datetime.datetime.now()),
    )

    db.define_table(
        'organization',
        Field('name', 'string'),
        Field('balance', 'float'),
        Field('subdomain', 'string', distinct = True),
    )

    db.define_table(
        'payment_history',
        Field('when', 'datetime', default = datetime.datetime.now()),
        Field('token', 'string'),
        Field('payment_validation_token', 'string'),
        Field('amount', 'float'),
        Field('status', 'string'),
        Field('organization', 'references organization'),
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
        Field('output', 'json')
    )
    return db