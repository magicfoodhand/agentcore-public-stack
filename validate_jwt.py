# Copyright 2017-2019 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You may not use this file
# except in compliance with the License. A copy of the License is located at
#
#     http://aws.amazon.com/apache2.0/
#
# or in the "license" file accompanying this file. This file is distributed on an "AS IS"
# BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations under the License.

import json
import sys
import time
import urllib.request

import boto3
from jose import jwk, jwt
from jose.utils import base64url_decode


def fetch_cognito_user_pools():
    """Fetch all Cognito user pools in the current AWS region via boto3."""
    client = boto3.client('cognito-idp')
    pools = []
    params = {'MaxResults': 60}
    while True:
        resp = client.list_user_pools(**params)
        pools.extend(resp.get('UserPools', []))
        next_token = resp.get('NextToken')
        if not next_token:
            break
        params['NextToken'] = next_token
    return pools


def select_pool_tui(pools):
    """Simple terminal UI for selecting a Cognito user pool."""
    print('\nMultiple Cognito user pools found. Select one:\n')
    for i, pool in enumerate(pools):
        print(f'  [{i + 1}] {pool["Name"]}  ({pool["Id"]})')
    print()

    while True:
        try:
            raw = input(f'Enter choice (1-{len(pools)}): ').strip()
            choice = int(raw)
            if 1 <= choice <= len(pools):
                return pools[choice - 1]
            print(f'  Please enter a number between 1 and {len(pools)}.')
        except (ValueError, EOFError):
            print(f'  Please enter a number between 1 and {len(pools)}.')


def resolve_user_pool():
    """Return (region, pool_id) by fetching Cognito pools.

    If exactly one pool exists it is used automatically.
    If multiple pools exist a TUI is presented for selection.
    """
    session = boto3.session.Session()
    region = session.region_name or 'us-east-1'

    pools = fetch_cognito_user_pools()

    if not pools:
        print('No Cognito user pools found in region', region)
        sys.exit(1)

    if len(pools) == 1:
        pool = pools[0]
        print(f'Using user pool: {pool["Name"]} ({pool["Id"]})')
    else:
        pool = select_pool_tui(pools)
        print(f'\nSelected: {pool["Name"]} ({pool["Id"]})')

    pool_id = pool['Id']
    # Pool ID format is <region>_<id>, extract region from it
    pool_region = pool_id.split('_')[0] if '_' in pool_id else region
    return pool_region, pool_id


def fetch_app_client_ids(pool_id):
    """Fetch all app client IDs for a given user pool."""
    client = boto3.client('cognito-idp')
    resp = client.list_user_pool_clients(UserPoolId=pool_id, MaxResults=60)
    return [c['ClientId'] for c in resp.get('UserPoolClients', [])]


def download_jwks(region, userpool_id):
    """Download the JWKS public keys for a Cognito user pool."""
    keys_url = f'https://cognito-idp.{region}.amazonaws.com/{userpool_id}/.well-known/jwks.json'
    with urllib.request.urlopen(keys_url) as f:
        response = f.read()
    return json.loads(response.decode('utf-8'))['keys']


def validate_jwt(token, keys, app_client_ids):
    """Validate a JWT token against the provided JWKS keys and app client IDs."""
    # get the kid from the headers prior to verification
    headers = jwt.get_unverified_headers(token)
    kid = headers['kid']

    # search for the kid in the downloaded public keys
    key_index = -1
    for i in range(len(keys)):
        if kid == keys[i]['kid']:
            key_index = i
            break
    if key_index == -1:
        print('Public key not found in jwks.json')
        return False

    # construct the public key
    public_key = jwk.construct(keys[key_index])

    # get the last two sections of the token,
    # message and signature (encoded in base64)
    message, encoded_signature = str(token).rsplit('.', 1)

    # decode the signature
    decoded_signature = base64url_decode(encoded_signature.encode('utf-8'))

    # verify the signature
    if not public_key.verify(message.encode('utf8'), decoded_signature):
        print('Signature verification failed')
        return False
    print('Signature successfully verified')

    # since we passed the verification, we can now safely
    # use the unverified claims
    claims = jwt.get_unverified_claims(token)

    # additionally we can verify the token expiration
    if time.time() > claims['exp']:
        print('Token is expired')
        return False
    print(claims)

    # and the Audience  (use claims['client_id'] if verifying an access token)
    aud = claims.get('aud') or claims.get('client_id')
    if aud not in app_client_ids:
        print('Token was not issued for this audience')
        return False

    # now we can use the claims
    return claims


def lambda_handler(event, context):
    """Entry point for AWS Lambda or local invocation."""
    region, userpool_id = resolve_user_pool()
    keys = download_jwks(region, userpool_id)
    app_client_ids = fetch_app_client_ids(userpool_id)

    if not app_client_ids:
        print(f'No app clients found for user pool {userpool_id}')
        return False

    token = event['token']
    return validate_jwt(token, keys, app_client_ids)


# the following is useful to make this script executable in both
# AWS Lambda and any other local environments
if __name__ == '__main__':
    # for testing locally you can enter the JWT ID Token here
    event = {'token': ''}
    lambda_handler(event, None)