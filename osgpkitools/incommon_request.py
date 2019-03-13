#!/usr/bin/python
# -*- coding: utf-8 -*-

"""
This script is used to submit multiple certificate requests to InCommon certificate service.
The intended user for the script is the Department Registration Authority Officer (DRAO) with SSL auto-approval and Certificate Auth enabled.

The DRAO must authenticate with  a user certificate issued by InCommon. The certificate must be configured for the DRAO in the InCommon Certificate Manager interface > Admins section. 

This script works in two modes:
1) Requesting single host certificate with -H option
2) Request multiple host certificates with hostnames stored in a file -f option

This script retrieves the certificates and output a set of files: hostname.key (private key) and hostname.pem (certificate)
"""

import httplib
import socket
import sys
import os
import time
import traceback
import json
import ConfigParser

import logging
logger = logging.getLogger('incommon_request')
logging.basicConfig()

from StringIO import StringIO
from ssl import SSLError
from optparse import OptionParser, OptionGroup

import utils
from ExceptionDefinitions import *
from rest_client import InCommonApiClient

MAX_RETRY_RETRIEVAL = 20
WAIT_RETRIEVAL= 5
WAIT_APPROVAL = 30

CONFIG_TEXT = """[InCommon]
organization: 9697
department: 9732
customeruri: InCommon
igtfservercert: 215
igtfmultidomain: 283
servertype: -1
term: 395
apiurl: cert-manager.com
listingurl: /private/api/ssl/v1/types
enrollurl: /private/api/ssl/v1/enroll  
retrieveurl: /private/api/ssl/v1/collect/
sslid: sslId
certx509: /x509
certx509co: /x509CO
certbase64: /base64
certbin: /bin
content_type: application/json
"""


# Set up Option Parser

ARGS = {}

def parse_args():
    """This function parses all the arguments, validates them and then stores them
    in a dictionary that is used throughout the script."""

    usage = \
'''Usage: %prog [--debug] -u username -k pkey -c cert \\
           (-H hostname | -f hostfile) [-a altnames] [-d write_directory]
       %prog [--debug] -u username -k pkey -c cert -T
       %prog -h
       %prog --version'''
    parser = OptionParser(usage, version=utils.VERSION_NUMBER)
    group = OptionGroup(parser, 'Hostname Options',
                        '''Use either of these options.
Specify hostname as a single hostname using -H/--hostname
or specify from a file using -f/--hostfile.''')
    group.add_option(
        '-H',
        '--hostname',
        action='store',
        dest='hostname',
        help='Specify the hostname or service/hostname for which you want to request ' + \
        'the certificate for. If specified, -f/--hostfile will be ignored',
        metavar='HOSTNAME',
        default=None,
        )
    group.add_option(
        '-f',
        '--hostfile',
        action='store',
        dest='hostfile',
        help='Filename with one host (hostname or service/hostname and its optional, ' + \
        'alternative hostnames, separated by spaces) per line',
        metavar='HOSTFILE',
        default=None,
        )
    parser.add_option(
        '-d',
        '--directory',
        action='store',
        dest='write_directory',
        help="Write the output files to this directory",
        default='.'
        )
    parser.add_option(
        '-k',
        '--pkey',
        action='store',
        dest='userprivkey',
        help="Specify Requestor's private key (PEM Format). If not specified " + \
             "will take the value of X509_USER_KEY or $HOME/.globus/userkey.pem",
        metavar='PKEY',
        default=None
    )
    parser.add_option(
        '-c',
        '--cert',
        action='store',
        dest='usercert',
        help="Specify requestor's user certificate (PEM Format). If not specified " + \
             "will take the value of X509_USER_CERT or $HOME/.globus/usercert.pem",
        metavar='CERT',
        default=None
    )
    parser.add_option(
        '-a',
        '--altname',
        action='append',
        dest='alt_names',
        help='Specify an alternative hostname for CSR (FQDN). May be used more than ' + \
             'once and if specified, -f/--hostfile will be ignored',
        metavar='HOSTNAME',
        default=[]
    )
    parser.add_option(
        '-u',
        '--username',
        action='store',
        dest='login',
        help='Provide the InCommon username (login).',
        metavar='LOGIN',
        default=[]
    )
    parser.add_option(
        '-T',
        '--test',
        action='store_true',
        dest='test',
        help='Test connection to InCommon API. Useful to test authentication credentials',
        default=False
    )
    parser.add_option(
        '',
        '--debug',
        dest='debug',
        help="Write debug output to stdout",
        action='store_true',
        default=False
    )
    
    parser.add_option_group(group)
    (args, values) = parser.parse_args()

    if args.debug:
        # this sets the root debug level
        logging.getLogger().setLevel(logging.DEBUG)
        logger.debug('Debug mode enabled')
    
    if not args.login:
        raise InsufficientArgumentException("InsufficientArgumentException: " + \
                                            "Please provide the InCommon username (login)\n")
        
    if not args.test:    
        if not args.hostname:
            if args.hostfile is None:
                raise InsufficientArgumentException("InsufficientArgumentException: " + \
                                                    "Please provide hostname(-H) or file name containing hosts(-f)\n")
            else:
                    hostfile = args.hostfile
        else:
            hostname = args.hostname
        
    if not args.test:
        if not args.hostname:
            if not os.path.exists(hostfile):
                raise FileNotFoundException(hostfile, 'Error: could not locate the hostfile')

    arguments = dict()

    if vars().has_key('args'):
        arguments.update({'args': args})
    if vars().has_key('values'):
        arguments.update({'values': values})
    if vars().has_key('hostname'):
        arguments.update({'hostname': hostname})
    
    arguments.update({'alt_names': args.alt_names})
    arguments.update({'test': args.test})
    arguments.update({'login': args.login})
    
    if not args.usercert or not args.userprivkey:
        raise InsufficientArgumentException("InsufficientArgumentException: " + \
                                            "Please provide certificate(-c, --cert) and key(-k, --pkey) files\n")
    
    usercert, userkey = utils.verify_user_cred(args.usercert, args.userprivkey)
    arguments.update({'usercert': usercert})
    arguments.update({'userprivkey': userkey})

    arguments.update({'certdir': args.write_directory})
    
    if vars().has_key('hostfile'):
        arguments.update({'hostfile': hostfile})
    
    return arguments

def build_headers(config):
    """"This function build the headers for the HTTP request.

        Returns headers for the HTTP request
    """
    
    headers = {
            "Content-type": str(config['content_type']), 
            "login": str(ARGS['login']), 
            "customerUri": str(config['customeruri']) 
    }

    return headers


def test_incommon_connection(config, restclient):
    """This function tests the connection to InCommon API
       and the credentials for authentication: cert and key.
       Performs a call to the listing SSL types endpoint. 
       Successful if response is HTTP 200 OK
    """
    # Build and update headers. Headers will be reused for all requests 
    headers = build_headers(config)
    response = None
    
    response = restclient.get_request(config['listingurl'], headers)
    response_text = response.read()
    logger.debug('response text: ' + str(response_text))
    try:
        if response.status == 200:
            utils.charlimit_textwrap("HTTP " + str(response.status) + " " + str(response.reason)) 
            utils.charlimit_textwrap("Successful connection to InCommon API")
        else:
            #InCommon API HTTP Error codes and messages are not consistent with documentation
            utils.charlimit_textwrap("HTTP " + str(response.status) + " " + str(response.reason))
            utils.charlimit_textwrap("Failed connection to InCommon API. Check your authentication credentials.")
    except httplib.HTTPException as exc:
        utils.charlimit_textwrap('InCommon API connection error')
        utils.charlimit_textwrap('Connection failure details: %s' % str(exc))
        utils.charlimit_textwrap('Check your configuration parameters or contact InCommon support.')
        

def submit_request(config, restclient, hostname, cert_csr, sans=None):
    """This function submits an enrollment request for a certificate
       If successful returns a self-enrollment certificate Id = sslId
    """
    # Build and update headers for the restclient 
    headers = build_headers(config)

    response = None
    response_data = None

    cert_type = config['igtfservercert']
    
    if sans:
        cert_type = config['igtfmultidomain']
    
    payload = dict(
        csr=cert_csr,
        orgId=config['department'],
        certType=cert_type,
        numberServers=0,
        serverType=config['servertype'],
        term=config['term'],
        comments="Certificate request for " + hostname
    )
   
    if sans:
        payload.update(subjAltNames=sans)
    
    try:
        response = restclient.post_request(config['enrollurl'], headers, payload)
        
        if response.status == 200:
            response_text = response.read()
            logger.debug('response text: ' + str(response_text))
            response_data = json.loads(response_text)
            response_data = response_data['sslId']
    except httplib.HTTPException as exc:
        raise
    
    return response_data
    
def retrieve_cert(config, sslcontext, sslId):
    """This function retrieves a certificate given a self-enrollment certificate Id = sslId
    """
    
    # Build and update headers for the restclient. Headers will be reused for all requests
    headers = build_headers(config)

    response = None
    response_data = None    

    retry_count = MAX_RETRY_RETRIEVAL
    retrieve_url = config['retrieveurl'] + str(sslId) + config['certx509co']
    
    for _ in range(retry_count):
        try:
            # If the HTTPSConnection is reused 
            restclient = InCommonApiClient(config['apiurl'], sslcontext)
            response = restclient.get_request(retrieve_url, headers)
            # InCommon API responds with 400 Bad Request when the certificate is still being procesed
            # "code": 0, "description": "Being processed by Sectigo"
            # It triggers the BadStatusLine exception avoiding to reuse the HTTPSConnection
            response_text = response.read()
            logger.debug('response text: ' + str(response_text))
            if response.status == 200:
                response_data = response_text
                restclient.closeConnection()
                break
        except httplib.BadStatusLine as exc:
            # BadStatusLine is raised as the server responded with a HTTP status code that we don't understand.
            pass
        except httplib.HTTPException as exc:
            raise
        utils.charlimit_textwrap('    Waiting for %s seconds before retrying certificate retrieval' % WAIT_RETRIEVAL )
        # Closing the connection before sleeping
        restclient.closeConnection()
        time.sleep(WAIT_RETRIEVAL)
    
    return response_data
           
def main():
    global ARGS
    try:	
        config_parser = ConfigParser.ConfigParser()
        config_parser.readfp(StringIO(CONFIG_TEXT))
        CONFIG = dict(config_parser.items('InCommon'))

        ARGS = parse_args()

        utils.check_permissions(ARGS['certdir'])
        
        # Creating SSLContext with cert and key provided
        # usercert and userprivkey are already validated by utils.findusercred
        ssl_context = utils.get_ssl_context(usercert=ARGS['usercert'], userkey=ARGS['userprivkey'])
        
        restclient = InCommonApiClient(CONFIG['apiurl'], ssl_context)

        if ARGS['test']:
            utils.charlimit_textwrap("Beginning testing mode: ignoring parameters.")
            test_incommon_connection(CONFIG, restclient)
            restclient.closeConnection()
            sys.exit(0)

        #Create tuple(s) either with a single hostname and altnames or with a set of hostnames and altnames from the hostfile
        if 'hostname' in ARGS:
            hosts = [tuple([ARGS['hostname'].strip()] + ARGS['alt_names'])]
        else:
            with open(ARGS['hostfile'], 'rb') as hosts_file:
                host_lines = hosts_file.readlines()
            hosts = [tuple(line.split()) for line in host_lines if line.strip()]
        
        requests = list()
        csrs = list()
        
        utils.charlimit_textwrap('Beginning request process for the following certificate(s):')
        utils.charlimit_textwrap('='*60)

        # Building the lists with certificates --> utils.Csr(object) 
        for host in set(hosts):
            common_name = host[0]
            sans = host[1:]
            
            utils.charlimit_textwrap('CN: %s, SANS: %s' % (common_name, sans))
            csr_obj = utils.Csr(common_name, ARGS['certdir'], altnames=sans)
            logger.debug(csr_obj.x509request.as_text())
            csrs.append(csr_obj)

        utils.charlimit_textwrap('='*60)

        for csr in csrs:
            subj = str(csr.x509request.get_subject())
            utils.charlimit_textwrap('Requesting certificate for %s' % subj)
            response_request = submit_request(CONFIG, restclient, subj, csr.base64_csr(), sans=csr.altnames)
            
            # response_request stores the sslId for the certificate request
            if response_request:
                requests.append(tuple([response_request, subj]))

                utils.charlimit_textwrap("Writing key file: %s" % csr.final_keypath)
                csr.write_pkey() 
        
        # Closing the restclient connection before going idle waiting for approval
        restclient.closeConnection()

        utils.charlimit_textwrap('Waiting %s seconds for certificate approval...' % WAIT_APPROVAL)
        time.sleep(WAIT_APPROVAL) 
        
        # Certificate retrieval has to retry until it gets the certificate
        # A restclient (InCommonApiClient) needs to be created for each retrieval attempt
        for request in requests:
            subj = request[1]
            utils.charlimit_textwrap('Retrieving certificate for %s' % subj)
            response_retrieve = retrieve_cert(CONFIG, ssl_context, request[0])

            if response_retrieve is not None:
                cert_path = os.path.join(ARGS['certdir'], subj.split("=")[1] + '-cert.pem')
                utils.charlimit_textwrap("Writing certificate file: %s" % cert_path)
                utils.safe_rename(cert_path)
                utils.atomic_write(cert_path, response_retrieve)
        
        utils.charlimit_textwrap("%s certificates were specified" % len(csrs))
        utils.charlimit_textwrap("%s certificates were requested and retrieved successfully" % len(requests))
        

    except SystemExit:
        raise
    except IOError as exc:
        utils.charlimit_textwrap('Certificate and/or key files were not found. More details below:')
        utils.print_exception_message(exc)
        sys.exit(1)
    except KeyboardInterrupt as exc:
        utils.print_exception_message(exc)
        sys.exit('''Interrupted by user\n''')
    except KeyError as exc:
        utils.charlimit_textwrap('Key %s not found' % exc)
        sys.exit(1)
    except FileWriteException as exc:
        utils.charlimit_textwrap(str(exc))
        sys.exit(1)
    except FileNotFoundException as exc:
        utils.charlimit_textwrap(str(exc) + ':' + exc.filename)
        sys.exit(1)
    except SSLError as exc:
        utils.print_exception_message(exc)
        sys.exit('Please check for valid certificate.\n')
    except (BadPassphraseException, AttributeError, EnvironmentError, ValueError, EOFError, SSLError) as exc:
        utils.charlimit_textwrap(str(exc))
        sys.exit(1)
    except InsufficientArgumentException as exc:
        utils.charlimit_textwrap('Insufficient arguments provided. More details below:')
        utils.print_exception_message(exc)
        sys.stderr.write("Usage: incommon-cert-request -h for help \n")
        sys.exit(1)
    except Exception:
        traceback.print_exc()
        sys.exit(1)
    sys.exit(0)
