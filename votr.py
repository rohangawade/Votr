from flask import Flask, render_template, request, flash, session, redirect, g
from flask import url_for
from flask_migrate import Migrate
from models import db, Users, Polls, Topics, Options, UserPolls
from flask_admin import Admin
from admin import AdminView, TopicView

import os
import config
import jwt
import requests
import uuid
from base64 import urlsafe_b64encode as url_encode

# Blueprints
from api.api import api
from dashboard.dashboard import dashboard

# celery
from celery import Celery

# Set env to loaded variables from config file
env = os.environ

import rollbar
import rollbar.contrib.flask
from flask import got_request_exception


def make_celery(app):
    celery = Celery(app.import_name)
    celery.conf.update(votr.config)
    TaskBase = celery.Task

    class ContextTask(TaskBase):
        abstract = True

        def __call__(self, *args, **kwargs):
            with app.app_context():
                return TaskBase.__call__(self, *args, **kwargs)
    celery.Task = ContextTask

    return celery

votr = Flask(__name__)

votr.register_blueprint(api)
votr.register_blueprint(dashboard)

# load config from the config file we created earlier
votr.config.from_object('config')

# create the database
db.init_app(votr)
# db.create_all(app=votr)

migrate = Migrate(votr, db, render_as_batch=True)

# create celery object
celery = make_celery(votr)

admin = Admin(votr, name='Dashboard',
              index_view=TopicView(Topics, db.session,
                                   url='/admin', endpoint='admin'))
admin.add_view(AdminView(Users, db.session))
admin.add_view(AdminView(Polls, db.session))
admin.add_view(AdminView(Options, db.session))
admin.add_view(AdminView(UserPolls, db.session))


# rollbar
@votr.before_first_request
def init_rollbar():
    """init rollbar module"""
    rollbar.init(
        # access token for the demo app: https://rollbar.com/demo
        env['ROLLBAR_TOKEN'],
        # environment name
        'votr',
        # server root directory, makes tracebacks prettier
        root=os.path.dirname(os.path.realpath(__file__)),
        # flask already sets up logging
        allow_logging_basic_config=False)

    # send exceptions from `app` to rollbar, using flask's signal system.
    got_request_exception.connect(rollbar.contrib.flask.report_exception, votr)

    @votr.before_request
    def init_template_variables():
        # make rollbar token available in template
        g.rollbar_token = env['ROLLBAR_TOKEN']


@votr.route('/')
# TODO Refactor and store variables in the session
def home():
    logout_url = request.url_root + 'logout'

    return render_template('index.html', logout_url=logout_url)


@votr.route('/callback')
def callback_handling():
    # get params from Auth0
    code = request.args.get(config.CODE_KEY)
    redirect_url = request.args.get('state')

    json_header = {config.CONTENT_TYPE_KEY: config.APP_JSON_KEY}
    token_url = 'https://{auth0_domain}/oauth/token'.format(
                    auth0_domain=env[config.AUTH0_DOMAIN])
    token_payload = {
        config.CLIENT_ID_KEY: env[config.AUTH0_CLIENT_ID],
        config.CLIENT_SECRET_KEY: env[config.AUTH0_CLIENT_SECRET],
        config.REDIRECT_URI_KEY: env[config.AUTH0_CALLBACK_URL],
        config.CODE_KEY: code,
        config.GRANT_TYPE_KEY: config.AUTHORIZATION_CODE_KEY
    }

    token_info = requests.post(token_url, json=token_payload,
                               headers=json_header).json()
    id_token = token_info['id_token']
    user_info = decode_jwt(id_token)

    if not user_info.get('email'):
        flash_message = "We could not get your email address from {} ."\
            "Please create an Email/Password account "\
            "or try another social signup.".\
            format(user_info['identities'][0]['provider'].capitalize())
        flash(flash_message, 'error')

        return render_template('index.html')

    # generate uuid and create a new user with a uuid, a better solution would
    # be to detect the signup or authenticate event and store the client_id as
    # the users metadata with Auth0
    email = user_info.get('email')
    user = Users.query.filter_by(email=email).first()

    if not user:
        client_id = url_encode(str(uuid.uuid4()).encode('utf-8')).decode()
        new_user = Users(email=email, client_id=client_id)
        db.session.add(new_user)
        db.session.commit()
        session['client_id'] = client_id
    else:
        session['client_id'] = user.client_id

    # store variables in session
    session[config.PROFILE_KEY] = user_info
    session['email'] = email
    session['id_token'] = id_token

    # used to redirect users to the specific poll page instead of the homepage
    if redirect_url:
        return redirect(redirect_url)

    email_verified = user_info.get('email_verified', False)
    if not email_verified:
        flash('We just sent a verification email to %s' % email, 'success')
        return redirect(url_for('home'))
    else:
        return redirect(url_for('dashboard.index'))


def decode_jwt(token):
    user_info = jwt.decode(token, env[config.AUTH0_CLIENT_SECRET],
                           audience=env[config.AUTH0_CLIENT_ID],
                           algorithms=['HS256'],
                           options={'verify_iat': False})

    return user_info


@votr.route('/logout')
def logout():
    if config.PROFILE_KEY in session:
        session.clear()
        flash('Thanks for using Votr!, We hope to see you soon', 'success')

    message = request.args.get('message', 'Not verified')
    success = request.args.get('success')

    if 'your email was verified' in message.lower() and success:
        flash('Your email has been verified. You can login now', 'success')

    return redirect(url_for('home'))


@votr.route('/new_poll', methods=['GET'])
def new_poll():
    return render_template('new_poll.html')


@votr.route('/polls/<unique_id>')
def poll(unique_id):

    return render_template('poll.html')


@votr.route('/embed/<unique_id>')
def embed(unique_id):

    return render_template('embed.html')
