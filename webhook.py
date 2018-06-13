# -*- coding: utf-8 -*-
# Required Libraries.
from __future__ import print_function
from flask import Flask, request, flash, render_template
from flask import Markup
from flask_basicauth import BasicAuth
from datetime import timedelta
from os import listdir
from os.path import isfile, join
from twilio.rest import Client
import oneagent.sdk 
# Singleton for the OneAgent
from oneagent.common import AgentState
import getpass
import sys
import os
import datetime
import json
import socket
import timeit
import logging
import subprocess
import requests
import traceback
from bottle import response

#######################
# Dynatrace Webhook API Custom Integration 
# based on Flask, a Python Web Microframework.
# This sample application receives a JSON Payload 
# and calls an executable on the OS with parameters, 
# it also sends an SMS and finally posts the results
# in the problem comments in Dynatrace for collaboration.
#######################

# Check OneAgent SDK initialization 
sdk = oneagent.sdk.SDK.get()
if sdk.agent_state == AgentState.ACTIVE:
    print('OneAgent is Active and hooked into this process.')
elif sdk.agent_state == AgentState.TEMPORARILY_INACTIVE:
    print('OneAgent is temporarily inactive but could be hooked later into this process.')
else:
    print('OneAgent is inactive or not available. No tracing active for this process.')

# Uptime variable
start_time = timeit.default_timer()

# Problems received variable
prob_count = 0

# API Endpoints 
API_ENDPOINT_PROBLEM_DETAILS = "/api/v1/problem/details/"
API_ENDPOINT_PROBLEM_FEED = "/api/v1/problem/feed/"
UI_PROBLEM_DETAIL_BY_ID = "/#problems/problemdetails;pid="

# Time intervals to poll prob_count via API
RELATIVETIMES = ['hour', '2hours', '6hours', 'day', 'week', 'month']

# JSON Files in memory
PROBLEMS_SENT = {}

# Read Configuration and assign the variables
config = json.load(open('config.json'))

# Tenant variables
TENANT_HOST = config['dynatrace']['tenant']
API_TOKEN = config['dynatrace']['api_token']

# Basic Authorization
USERNAME = config['webhook']['username']
PASSWORD = config['webhook']['password']

# Let the Microservice listen to all interfaces 
# to the port of your choice with 0.0.0.0
WEBHOOK_INTERFACE = config['webhook']['interface']
WEBHOOK_PORT = config['webhook']['port']
WEBHOOK_USERNAME = getpass.getuser()

# Program to call with the notification
EXEC_WIN = config['incident_notification']['exec_win']
EXEC_UNIX = config['incident_notification']['exec_unix']
INCIDENT_NOTIFICATION = config['incident_notification']['active']

SMS_NOTIFICATION = config['sms_notification']['active']
TWILIO_ACCOUNT = config['sms_notification']['twilio_account']
TWILIO_TOKEN = config['sms_notification']['twilio_token']
TWILIO_NUMBER = config['sms_notification']['twilio_number']
TO_NUMBER = config['sms_notification']['to_number']

LOGFILE = config['log_file']
LOGDIR = config['log_dir']

# Directory where the received JSON Problems are saved
DIR_RECEIVED = config['dir_received']
# Directory where the sent Problems are saved (full details of the problem)
DIR_SENT = config['dir_sent']


def check_create_dir(dir_name):
    if not os.path.exists(dir_name):
        os.makedirs(dir_name)


# Logging configuration
# Create log directory at initialization
check_create_dir(LOGDIR)
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', filename=LOGDIR + '/' + LOGFILE,
                    level=logging.INFO)

# Add logging also to the console of the running program
logging.getLogger().addHandler(logging.StreamHandler())
# Set Twilio logging to warning. 
logging.getLogger("twilio").setLevel(logging.WARNING)

# Create Application for the OneAgent
sdkapp = sdk.create_web_application_info(
    virtual_host="https://{0}:{1}".format(WEBHOOK_INTERFACE, WEBHOOK_PORT),
    application_id='Custom Python Webhook',
    context_root='/')

# Initiate Flask Microservice with basic authentication
app = Flask(__name__)
app.secret_key = "super_secret_key"
app.config['BASIC_AUTH_USERNAME'] = USERNAME
app.config['BASIC_AUTH_PASSWORD'] = PASSWORD

# Protect your entire site with basic access authentication,
# app.config['BASIC_AUTH_FORCE'] = True
basic_auth = BasicAuth(app)


# Flask listener for POST Method with authorization
@app.route('/', methods=['POST'])
@basic_auth.required
def handle_post():
    try:
        h = request.headers
        wreq = sdk.trace_incoming_web_request(sdkapp, request.full_path, request.method, headers={'Connection':h.get('Connection'), 'Accept':h.get('Accept'), 'Host': h.get('Host'), 'Cache-Control':h.get('Cache-Control')},
                                               remote_address=request.remote_addr)
        with wreq:
            wreq.add_parameter('user-agent', str(request.user_agent))
            wreq.add_parameter('Client IP', str(request.remote_addr))
            global prob_count 
            prob_count += 1
            problem_simple = json.loads(request.data)
            logging.info('Notification received from ' + request.remote_addr);
            
            # JSON Payload will be saved in a directory
            save_request(problem_simple)
            
            # show the problem Id in Purepath
            wreq.add_parameter('ProblemID', problem_simple['ProblemID'])
            
            # If Test Msg do not call integration
            if "999" in problem_simple['ProblemID']:
                logging.info('Test message successfully received. No integration will be called')
                wreq.add_response_headers(response.headers)
                wreq.set_status_code(response.status_code)
                return "OK"
            
            # Integrations will be called
            call_integration(problem_simple['PID'])
            
            wreq.add_response_headers(response.headers)
            wreq.set_status_code(response.status_code)
    except Exception as e:
        logging.error("There was an error handling the Request")
        logging.error(traceback.format_exc())
    return "OK"


# Will return the uptime in seconds, minutes, hours and days
def get_uptime():
    return str(timedelta(seconds=timeit.default_timer() - start_time))


def break_dic_in_rows(values):
    row = ''
    for key, value in values.items():
        row += key + ':' + str(value) + '<br>'
    return row


def break_list_in_rows(values):
    row = ''
    for value in values:
        row += str(value) + '<br>'
    return row


def get_timestamp_to_date(millisec):
    if millisec < 0 :
        return 'still open'
    else: 
        sec = millisec / 1000
        return datetime.datetime.utcfromtimestamp(sec).strftime('%Y-%m-%d %H:%M:%S')


# Will return either a date, string, list or an html table
def get_proper_value(key, value):
    
    if 'id' == key:
        return '<a target="_blank" href="{0}{1}{2}">open in Dynatrace</a>'.format(TENANT_HOST, UI_PROBLEM_DETAIL_BY_ID, value)
    
    if 'time' in key.lower() and isinstance(value, int):
        return get_timestamp_to_date(value)
    
    if isinstance(value, dict):
        return break_dic_in_rows(value) 
    
    elif isinstance(value, (list, tuple)):
        if len(value) > 0:
            if isinstance(value[0], dict):
                return get_table_from_list(value)
            
            # List inside list
            elif isinstance(value[0], list): 
                return str(value)
            # List of strings
            elif isinstance(value[0], str):
                return break_list_in_rows(value)
            else: 
                return str(value)
        
        return str(value)
    else:
        return str(value)


# Returns an HTML table from a dictionary
def get_table_from_list(items):
    rows = ''
    i = 0
    for item in items:
        i = i + 1
        td = '<tr>'
        if i == 1:
            th = '<tr>'
        for key, value in item.items():
            td += '<td>' + get_proper_value(key, value) + '</td>'
            if i == 1:
                th += '<th>' + key + '</th>'
            
        td += '</tr>'
        if i == 1:
            th += '</tr>'
            rows += th + td
        else:
            rows += td
    return '<div style="overflow-x:auto;"><table>' + rows + '</table></div>'
    

def get_table():
    # TODO: Get only the table when a problem comes. save it in memory and actualize it?
    if len(PROBLEMS_SENT.values()) == 0:
        return 'No problems polled nor received.'
    
    # Populate the table
    table = ''
    try:
        table = get_table_from_list(PROBLEMS_SENT.values())
    except Exception as e:
        logging.error("There was an error generating the html Table:" + str(e))
        logging.error(traceback.format_exc())
        
    return table


def get_buttons_from_relativetimes():
    buttons = ''
    for t in RELATIVETIMES:
        buttons += "<button onclick=\"window.location.href='?relativeTime={0}'\">{0}</button>&nbsp;".format(t)
    return buttons


# Flask listener for GET Method
# with public access
@app.route('/', methods=['GET'])
def handle_get():
    
    h = request.headers
    wreq = sdk.trace_incoming_web_request(sdkapp, request.full_path, request.method, headers={'Connection':h.get('Connection'), 'Accept':h.get('Accept'), 'Host': h.get('Host'), 'Cache-Control':h.get('Cache-Control')},
    remote_address=request.remote_addr)

    with wreq:
        wreq.add_parameter('user-agent', str(request.user_agent))
        wreq.add_parameter('Client IP', str(request.remote_addr))
        # Process web request
        time_option = request.args.get('relativeTime')
        flash(Markup("<br>Python Flask Webhook endpoint: " + TENANT_HOST + "</br>"))
        flash(Markup("<br>Flask Web Microservice running on: https://{0}:{1}".format(WEBHOOK_INTERFACE, WEBHOOK_PORT)))
        flash(Markup("<br>Received notifications: {0}".format(prob_count)))
        flash(Markup("<br>Path: {0}".format(os.getcwd())))
        flash(Markup("<br>Host: {0}".format(socket.gethostname())))
        flash(Markup("<br>User: {0}".format(WEBHOOK_USERNAME)))
        flash(Markup("<br>PID: {0}".format(os.getpid())))
        flash(Markup("<br>Uptime: {0}".format(get_uptime())))
        # TODO JQuery efect
        flash(Markup("<br><button onclick=\"showHideById('usage')\">toggle usage</button>"))
        flash(Markup("<div id=\"usage\" class=\"usage\">"))
        flash(Markup("{0}".format(get_usage_as_html())))
        flash(Markup("</div>"))
        flash(Markup("<br><br>Poll the problems via API for the last:"))
        flash(Markup(get_buttons_from_relativetimes()))
            
        if time_option:
            data = get_problemsfeed_by_time(time_option)
            flash(Markup("<br><button onclick=\"showHideById('table_poll')\">toggle poll table</button>"))
            flash(Markup("<div id=\"table_poll\">"))
            flash(Markup("<br>There were {0} problems found during the selected timeframe '{1}'".format(
                    len(data['result']['problems']), time_option)))
            flash(Markup("<br>Dynatrace has monitored the following entities in the last '{0}':".format(time_option)))
            flash(Markup("<br>APPLICATION:\t {:6}".format(data['result']['monitored']['APPLICATION'])))
            flash(Markup("<br>SERVICE:\t {:6}".format(data['result']['monitored']['SERVICE'])))
            flash(Markup("<br>INFRASTRUCTURE:\t {:6}".format(data['result']['monitored']['INFRASTRUCTURE'])))
            flash(Markup(get_table_from_list(data['result']['problems'])))
            flash(Markup("</div>"))
            
        flash(Markup("<br><br><button onclick=\"showHideById('table_saved')\">toggle sent table</button>"))
        flash(Markup("<div id=\"table_saved\">"))
        flash(Markup("Successfully sent problems (saved in {0}\{1}):".format(os.getcwd(), DIR_SENT)))
        flash(Markup("<br>" + get_table()))
        flash(Markup("</div>"))
        
        template = render_template('index.html')
        
        wreq.add_response_headers(response.headers)
        wreq.set_status_code(response.status_code)
    
    return template


def get_usage_as_html():
    str_with_breaks = ''
    for line in get_usage_as_string().splitlines():
        str_with_breaks += line + '</br>'
    return str_with_breaks


# For handling Tenants with an invalid SSL Certificate just set it to false.
def verifyRequest():
    return True


def handle_response_status(msg, response):
    if response.status_code != 200:
        err_msg = "There was an {0} error {1}. HTTP CODE[{2}] \n " \
                  "Response Payload:{3}".format(response.reason, msg, response.status_code, response.content)
        logging.error(err_msg)
        raise Exception(err_msg)


def getAuthenticationHeader():
    return {"Authorization": "Api-Token " + API_TOKEN}


def get_problemsfeed_by_time(time_option):
    outcall = sdk.trace_outgoing_remote_call(
    'get_problemsfeed_by_time', 'Dynatrace API', API_ENDPOINT_PROBLEM_FEED + "?relativeTime=" + time_option, oneagent.sdk.Channel(oneagent.sdk.ChannelType.TCP_IP, TENANT_HOST),
    protocol_name='HTTP/custom')
    with outcall:
        msg = "fetching prob_count for '" + time_option + "' - " + API_ENDPOINT_PROBLEM_FEED
        logging.info(msg)
        response = requests.get(TENANT_HOST + API_ENDPOINT_PROBLEM_FEED + "?relativeTime=" + time_option,
                                    headers=getAuthenticationHeader(), verify=verifyRequest())
        
        handle_response_status(msg, response)
        data = json.loads(response.text)
        logging.debug('Response content: ' + str(response.content)) 
    return data


def get_problem_by_id(problemid):
    outcall = sdk.trace_outgoing_remote_call(
    'get_problem_by_id', 'Dynatrace API', API_ENDPOINT_PROBLEM_DETAILS + problemid, oneagent.sdk.Channel(oneagent.sdk.ChannelType.TCP_IP, TENANT_HOST),
    protocol_name='HTTP/custom')
    with outcall:
        msg = "fetching problem id " + str(problemid)
        logging.info(msg)
        response = requests.get(TENANT_HOST + API_ENDPOINT_PROBLEM_DETAILS + problemid, headers=getAuthenticationHeader(),
                            verify=verifyRequest())
        handle_response_status(msg, response)
        data = json.loads(response.text)
        logging.info("Problem ID " + problemid + " fetched")
    return data['result']


def is_new_problem(problem):
    if problem['displayName'] in PROBLEMS_SENT:
        if PROBLEMS_SENT.get(problem['displayName'])['status'] == problem['status']:
            logging.info(
                "Problem {0} has already been submitted to the Incident Software. To do it again delete the file {0}.json in the directory {1}".format(problem['displayName'], DIR_SENT))
            return False
        else:
            return True
    # Problem is not in the Dictionary
    return True


def get_program_argument(problem_details):
    """
    In here you can make the mapping and translation of the different
    parameter values and attributes for the Incident Software of your desire
    """
    nr = problem_details['displayName']
    status = problem_details['status']
    severity = problem_details['severityLevel']
    element = problem_details['impactLevel']
    tags = problem_details['tagsOfAffectedEntities']
    
    msg = "Problem [{0}]: Status={1}, Severity={2}, ImpactLevel={3}, Tags={4}".format(nr, status, severity, element, tags) 
    
    # Get the elements. Key from ProblemID differs from ProblemFeed (rankedImpacts/rankedEvents)
    if 'rankedImpacts' in problem_details:
        elements = problem_details['rankedImpacts']
    else:
        elements = problem_details['rankedEvents']
         
    # For each ranked Impact (Entity), a call to the Incident Software shall be made
    arguments_list = []
    for element in elements:
        e_name = element['entityName']
        e_severity = element['severityLevel']
        e_impact = element['impactLevel']
        e_eventType = element['eventType']
        element_msg = msg
        element_msg += " Entity details: Entity={0}, impactLevel={1}, severity={2}, eventType={3}".format(e_name, e_severity, e_impact, e_eventType) 
        arguments_list.append(element_msg)
        
    return arguments_list


def post_incident_result_in_problem_comments(problem, return_code, error):
    problemNr = problem['displayName']
    logging.info('Problem {0} will be commented in Dynatrace'.format(problemNr))
    data = {}
    if error:
        data['comment'] = "The problem {0} could not been sent to the Incident Software. Return Codes[{1}]".format(problemNr, return_code)
    else:
        data['comment'] = "The problem {0} has been sent to the Incident Software. Calls made:{1}".format(problemNr, len(return_code))

    data['user'] = WEBHOOK_USERNAME
    data['context'] = 'Incident Software Custom Integration'
    
    r = post_in_comments(problem, data);

    if r.status_code == 200:
        logging.info('Problem {0} was commented successfully in Dynatrace'.format(problemNr))
        logging.debug('Content:{1}'.format(problemNr, data))
    else:
        logging.error(
            'Problem {0} could not be commented in Dynatrace. Reason {1}-{2}. Content:{3}'.format(problemNr,
                                                                                                          r.reason,
                                                                                                          r.status_code,
                                                                                                          data))
    return


def post_in_comments(problem, data):
    outcall = sdk.trace_outgoing_remote_call(
    'post_in_comments', 'Dynatrace API', API_ENDPOINT_PROBLEM_DETAILS + problem['id'] + "/comments", oneagent.sdk.Channel(oneagent.sdk.ChannelType.TCP_IP, TENANT_HOST),
    protocol_name='HTTP/custom')
    with outcall:
        # Define header
        headers = {'content-type': 'application/json', "Authorization": "Api-Token " + API_TOKEN}
        # Make POST Request
        r = requests.post(TENANT_HOST + API_ENDPOINT_PROBLEM_DETAILS + problem['id'] + "/comments", json=data,
                          headers=headers, verify=verifyRequest())
        # Return response
    return r


# In this method are the integrations defined and called
def call_integration(problem_id):
    
    # Fetch all the details of the Problem
    problem_details = get_problem_by_id(problem_id)
        
    # Notify the incident software and comment the result in Dynatrace
    if INCIDENT_NOTIFICATION:
        call_incident_software(problem_details)
    
    # Send an SMS message and comment in Dynatrace
    if SMS_NOTIFICATION: 
        call_sms_integration(problem_details)
    
    # Problems will be sent two times, when open and closed.
    # Update the dictionary e.g. when a Problem is closed. The problemNr is the key of the dictionary
    PROBLEMS_SENT[problem_details["displayName"]] = problem_details
    
    # Persist the sent notifications
    persist_problem(problem_details)
    return


def call_sms_integration(problem_details):
    outcall = sdk.trace_outgoing_remote_call(
    'call_sms_integration', 'TwilioService', 'Twilio API' , oneagent.sdk.Channel(oneagent.sdk.ChannelType.TCP_IP, TWILIO_NUMBER),
    protocol_name='SMS/custom')
    with outcall:
        # SMS Client initialized with the Twilio Account (SID and Auth-Token)
        sms_client = Client(TWILIO_ACCOUNT, TWILIO_TOKEN)
        
        level = problem_details["impactLevel"]
        nr = problem_details["displayName"]
        pid = problem_details["id"]
        status = problem_details["status"]
        
        # change the "from_" number to your Twilio number and the "to" number
        # to the phone number you signed up for Twilio with, or upgrade your
        # account to send SMS to any phone number
        body = "Dynatrace notification - {0} problem ({1}) {5}. Open in Dynatrace:{2}{3}{4}".format(level.lower(), nr, TENANT_HOST, UI_PROBLEM_DETAIL_BY_ID, pid, status.lower())
        sms_client.messages.create(to=TO_NUMBER, from_=TWILIO_NUMBER, body=body)
         
        TO_NUMBER
        # Post SMS result in the comments
        data = {}
        data['comment'] = "Mobile number has been notified: {0}".format(anonymize_numer(TO_NUMBER))
        data['user'] = WEBHOOK_USERNAME
        data['context'] = 'Twilio Custom Integration'
        r = post_in_comments(problem_details, data)
        # Log to the console
        logging.info('{0}: {1} sent from {2}'.format(data['context'] , data['comment'], TWILIO_NUMBER))
    return


def anonymize_numer(number):
    return str(number[0:3] + '*****' + number[-4:])


def call_incident_software(problem_details):
    problem_nr = problem_details['displayName']
    # Check the OS of the program to call (Windows or Linux)
    if os.name == 'nt':
        EXECUTABLE = EXEC_WIN
    else:
        EXECUTABLE = EXEC_UNIX
    
    outcall = sdk.trace_outgoing_remote_call(
    'call_incident_software', 'LegacyService', os.getcwd() , oneagent.sdk.Channel(oneagent.sdk.ChannelType.TCP_IP, EXECUTABLE),
    protocol_name='CLI/custom')

    with outcall:
        argument_list = get_program_argument(problem_details)
        
        return_codes = []
        for argument in argument_list:
            return_code = (subprocess.call(EXECUTABLE + ' ' + argument, shell=True))
            logging.info('Incident Software call for [{0}] RC[{1}] Executable:[{2}] Arguments:{3}'.format(str(problem_nr), return_code, EXECUTABLE, argument))
            return_codes.append(return_code)
    
        # Check all the return codes
        for r in return_codes:
            r += r
        # If the sum is bigger than zero, a problem occurred when calling the Incident software.
        if r == 0:
            logging.info('All calls to the Incident Software for [{0}] OK, Return Codes{1}'.format(problem_nr, return_codes))
            post_incident_result_in_problem_comments(problem_details, return_codes, False)
            
        else:
            logging.error('There was a problem calling the Incident Software [{0}], Return Codes{1}'.format(problem_nr, return_codes))
            post_incident_result_in_problem_comments(problem_details, return_codes, True)
    return


# This will save the json notification in a directory
def save_request(data):
    if not os.path.exists(DIR_RECEIVED):
        os.makedirs(DIR_RECEIVED)
    problemnr = data['ProblemID']
    state = data['State']
    filename = problemnr + '-' + state + '.json'
    with open(DIR_RECEIVED + '/' + filename, 'w') as f:
        json.dump(data, f, ensure_ascii=False)
    return


# Poll the errors
def poll_problems(time_option):
    logging.info("----------------------------------------------")
    logging.info("Polling problems for {0}{1} with Key'{2}' and relativeTime '{3}'".format(TENANT_HOST,
                                                                                             API_ENDPOINT_PROBLEM_FEED,
                                                                                             API_TOKEN, time_option))
    try:
        data = get_problemsfeed_by_time(time_option)
        # Print the amount of errors and monitored entities.
        logging.info("There were {0} problems found during the selected timeframe '{1}'".format(
            len(data['result']['problems']), time_option))
        logging.info("Dynatrace has monitored the following entities in the last '{0}':".format(time_option))
        logging.info("APPLICATION:\t {:6}".format(data['result']['monitored']['APPLICATION']))
        logging.info("SERVICE:\t {:6}".format(data['result']['monitored']['SERVICE']))
        logging.info("INFRASTRUCTURE:\t {:6}".format(data['result']['monitored']['INFRASTRUCTURE']))

        if (data and data['result']['problems']):
            for problem_details in data['result']['problems']:
                if is_new_problem(problem_details):
                    call_integration(problem_details['id'])
    except Exception as e:
        logging.error("There was an error polling the problems")
        logging.error(traceback.format_exc())
    return


def persist_problem(problem_details):
    check_create_dir(DIR_SENT)
    filename = DIR_SENT + '/' + problem_details["displayName"] + '.json'
    with open(filename, 'w') as f:
        json.dump(problem_details, f)


def load_problems():
    global PROBLEMS_SENT
    check_create_dir(DIR_SENT)
    jsonfiles = [f for f in listdir(DIR_SENT) if isfile(join(DIR_SENT, f))]
    for file in jsonfiles:
        with open(DIR_SENT + '/' + file, 'r') as f:
            problem_details = json.load(f)
            PROBLEMS_SENT[problem_details["displayName"]] = problem_details
            

def main():
    
    load_problems()
    
    logging.info("\nDynatrace Custom Webhook Integration")
    command = ""
    printUsage = False
    if len(sys.argv) >= 2:
        command = sys.argv[1]
        
        if command == "run":
            logging.info("----------------------------------------------")
            logging.info("\nStarting the Flask Web Microservice")
            app.run(host=WEBHOOK_INTERFACE, port=WEBHOOK_PORT)

        elif command == "poll":
            if len(sys.argv) == 3:
                option = sys.argv[2]
                if option in RELATIVETIMES:
                    poll_problems(option)
                else:
                    printUsage = True

            elif len(sys.argv) == 2:
                poll_problems(RELATIVETIMES[0])
            else:
                printUsage = True
        else:
            printUsage = True
    else:
        printUsage = True

    if printUsage:
        doUsage(sys.argv)
    else:
        print("Bye")
    exit


def get_usage_as_string():
    return """
Dynatrace Custom Webhook Integration
=======================================================
Usage: webhook.py <command> <options>
commands: help = Prints this options
commands: run  = Starts the WebServer.
commands: poll = Polls the Problems found in the API and calls the Incident Software. Default time hour.
commands: poll <options>: relativeTime (optional) Possible values: hour, 2hours, 6hours, day, week, month
=======================================================
"""


def doUsage(args):
    "Just printing Usage"
    usage = get_usage_as_string()
    print(usage)
    exit


# Start Main
if __name__ == "__main__": main()

