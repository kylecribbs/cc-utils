# Copyright (c) 2018 SAP SE or an SAP affiliate company. All rights reserved. This file is licensed
# under the Apache Software License, v. 2 except as noted otherwise in the LICENSE file
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import base64
import json
import time
from urllib3.exceptions import ProtocolError

import kubernetes
from kubernetes import watch
from kubernetes.client import (
    CoreV1Api, AppsV1Api, ExtensionsV1beta1Api, V1ObjectMeta, V1Secret, V1ServiceAccount,
    V1LocalObjectReference, V1Namespace, V1Service, V1Deployment,
    V1beta1Ingress,
)
from kubernetes.client.rest import ApiException
from kubernetes.stream import stream
from ensure import ensure_annotations

from util import info, not_empty, not_none, fail


class KubernetesSecretHelper(object):
    '''Helper class for handling kubernetes secret objects'''
    @ensure_annotations
    def __init__(self, core_api: CoreV1Api):
        self.core_api = core_api

    def create_gcr_secret(
        self,
        namespace: str,
        name: str,
        password: str,
        email: str,
        user_name: str='_json_key',
        server_url: str='https://eu.gcr.io'
      ):
        metadata = V1ObjectMeta(name=name, namespace=namespace)
        secret = V1Secret(metadata=metadata)

        auth = '{user}:{gcr_secret}'.format(
          user=user_name,
          gcr_secret=password
        )

        docker_config = {
          server_url: {
            'username': user_name,
            'email': email,
            'password': password,
            'auth': base64.b64encode(auth.encode('utf-8')).decode('utf-8')
          }
        }

        encoded_docker_config = base64.b64encode(
          json.dumps(docker_config).encode('utf-8')
        ).decode('utf-8')

        secret.data = {
          '.dockercfg': encoded_docker_config
        }
        secret.type = 'kubernetes.io/dockercfg'

        self.core_api.create_namespaced_secret(namespace=namespace, body=secret)

    def put_secret(self, name: str, data: dict, namespace: str='default'):
        '''creates or updates (replaces) the specified secret.
        the secret's contents are expected in a dictionary containing only scalar values.
        In particular, each value is converted into a str; the result returned from
        to-str conversion is encoded as a utf-8 byte array. Thus such a conversion must
        not have done before.
        '''
        ne = not_empty
        metadata = V1ObjectMeta(name=ne(name), namespace=ne(namespace))

        secret_data = {
            k: base64.b64encode(str(v).encode('utf-8')).decode('utf-8')
            for k,v in data.items()
        }

        secret = V1Secret(metadata=metadata, data=secret_data)

        # find out whether we have to replace or to create
        try:
            self.core_api.read_namespaced_secret(name=name, namespace=namespace)
            secret_exists = True
        except ApiException as ae:
            # only 404 is expected
            if not ae.status == 404:
                raise ae
            secret_exists = False

        if secret_exists:
            self.core_api.replace_namespaced_secret(name=name, namespace=namespace, body=secret)
        else:
            self.core_api.create_namespaced_secret(namespace=namespace, body=secret)

    def get_secret(self, name: str, namespace: str) -> V1Secret:
        '''Returns the `V1Secret` with the given name in the given namespace, or `None`'''
        try:
            secret = self.core_api.read_namespaced_secret(name=name, namespace=namespace)
        except ApiException as ae:
            if not ae.status == 404:
                raise ae
            else:
                return None
        return secret


class KubernetesServiceAccountHelper(object):
    '''Helper class for kubernetes service-account objects'''

    def __init__(self, core_api: CoreV1Api):
        self.core_api = core_api

    def patch_image_pull_secret_into_service_account(
        self, name: str,
        namespace: str,
        image_pull_secret_name: str
      ):
        '''Patches the given (by name) image-pull-secret into the specified service-account.'''
        service_account = V1ServiceAccount()
        reference = V1LocalObjectReference()
        reference.name = image_pull_secret_name
        service_account.image_pull_secrets = [reference]
        self.core_api.patch_namespaced_service_account(
            name=name,
            namespace=namespace,
            body=service_account
        )


class KubernetesNamespaceHelper(object):
    '''Helper class for kubernetes namespace objects'''

    @ensure_annotations
    def __init__(self, core_api: CoreV1Api):
        self.core_api = core_api

    def create_namespace(self, namespace: str):
        '''Creates a new namespace and returns it'''
        not_empty(namespace)
        metadata = V1ObjectMeta(name=namespace)
        ns = V1Namespace(metadata=metadata)
        return self.core_api.create_namespace(ns)

    def create_if_absent(self, namespace: str):
        '''Create a new namespace iff it does not already exist'''
        not_empty(namespace)

        existing_namespace = self.get_namespace(namespace)
        if not existing_namespace:
            self.create_namespace(namespace)

    @ensure_annotations
    def delete_namespace(self, namespace: str):
        not_empty(namespace)
        self.core_api.delete_namespace(name=namespace, body={})

    def get_namespace(self, namespace: str):
        '''Returns the `V1Namespace` corresponding to the given name, or `None`'''
        for ns in self.core_api.list_namespace().items:
            # check if 'tis our namespace
            name = ns.metadata.name
            if not name == namespace:
                continue
            return ns
        return None


class KubernetesServiceHelper(object):
    def __init__(self, core_api: CoreV1Api):
        self.core_api = core_api

    def replace_or_create_service(self, namespace: str, service: V1Service):
        '''Create a service in a given namespace. If the service already exists,
        the previous version will be deleted beforehand
        '''
        not_empty(namespace)
        not_none(service)

        service_name = service.metadata.name
        existing_service = self.get_service(namespace=namespace, name=service_name)
        if existing_service:
            self.core_api.delete_namespaced_service(namespace=namespace, name=service_name)
        self.create_service(namespace=namespace, service=service)

    def create_service(self, namespace: str, service: V1Service):
        '''Create a service in a given namespace. Raises an `ApiException` if such a Service
        already exists.
        '''
        not_empty(namespace)
        not_none(service)

        self.core_api.create_namespaced_service(namespace=namespace, body=service)

    def get_service(self, namespace: str, name: str) -> V1Service:
        '''Return the `V1Service` with the given name in the given namespace, or `None` if
        no such service exists.
        '''
        not_empty(namespace)
        not_empty(name)

        try:
            service = self.core_api.read_namespaced_service(name=name, namespace=namespace)
        except ApiException as ae:
            if ae.status == 404:
                return None
            raise ae
        return service


class KubernetesDeploymentHelper(object):
    def __init__(self, apps_api: AppsV1Api):
        self.apps_api = apps_api

    def replace_or_create_deployment(self, namespace: str, deployment: V1Deployment):
        '''Create a deployment in a given namespace. If the deployment already exists,
        the previous version will be deleted beforehand.
        '''
        not_empty(namespace)
        not_none(deployment)

        deployment_name = deployment.metadata.name
        existing_deployment = self.get_deployment(namespace=namespace, name=deployment_name)
        if existing_deployment:
            self.apps_api.delete_namespaced_deployment(
                namespace=namespace,
                name=deployment_name,
                body=kubernetes.client.V1DeleteOptions()
            )
        self.create_deployment(namespace=namespace, deployment=deployment)

    def create_deployment(self, namespace: str, deployment: V1Deployment):
        '''Create a deployment in a given namespace. Raises an `ApiException` if such a deployment
        already exists.'''
        not_empty(namespace)
        not_none(deployment)

        self.apps_api.create_namespaced_deployment(namespace=namespace, body=deployment)

    def get_deployment(self, namespace: str, name: str) -> V1Deployment:
        '''Return the `V1Deployment` with the given name in the given namespace, or `None` if
        no such deployment exists.'''
        not_empty(namespace)
        not_empty(name)

        try:
            deployment = self.apps_api.read_namespaced_deployment(name=name, namespace=namespace)
        except ApiException as ae:
            if ae.status == 404:
                return None
            raise ae
        return deployment

    def patch_deployment(self, name: str, namespace: str, body: dict):
        '''Patches a deployment with a given name in the given namespace.'''
        not_empty(name)
        not_empty(namespace)
        not_empty(body)

        if not self.get_deployment(namespace, name):
            fail(f'Deployment {name} in namespace {namespace} does not exist')

        self.apps_api.patch_namespaced_deployment(name, namespace, body)

    def wait_until_deployment_available(self, namespace: str, name: str, timeout_seconds: int=60):
        '''Block until the given deployment has at least one available replica (or timeout)
        Return `True` if the deployment is available, `False` if a timeout occured.
        '''
        not_empty(namespace)
        not_empty(name)

        w = watch.Watch()
        # Work around IncompleteRead errors resulting in ProtocolErrors - no fault of our own
        start_time = int(time.time())
        while (start_time + timeout_seconds) > time.time():
            try:
                for event in w.stream(
                    self.apps_api.list_namespaced_deployment,
                    namespace=namespace,
                    timeout_seconds=timeout_seconds
                ):
                    deployment_spec = event['object']
                    if deployment_spec is not None:
                        if deployment_spec.metadata.name == name:
                            if deployment_spec.status.available_replicas is not None \
                                    and deployment_spec.status.available_replicas > 0:
                                return True
                    # Check explicitly if timeout occurred
                    if (start_time + timeout_seconds) < time.time():
                        return False
                # Regular Watch.stream() timeout occurred, no need for further checks
                return False
            except ProtocolError:
                info('http connection error - ignored')


class KubernetesIngressHelper(object):
    def __init__(self, extensions_v1beta1_api: ExtensionsV1beta1Api):
        self.extensions_v1beta1_api = extensions_v1beta1_api

    def replace_or_create_ingress(self, namespace: str, ingress: V1beta1Ingress):
        '''Create an ingress in a given namespace. If the ingress already exists,
        the previous version will be deleted beforehand.
        '''
        not_empty(namespace)
        not_none(ingress)

        ingress_name = ingress.metadata.name
        existing_ingress = self.get_ingress(namespace=namespace, name=ingress_name)
        if existing_ingress:
            self.extensions_v1beta1_api.delete_namespaced_ingress(
                namespace=namespace,
                name=ingress_name,
                body=kubernetes.client.V1DeleteOptions()
            )
        self.create_ingress(namespace=namespace, ingress=ingress)

    def create_ingress(self, namespace: str, ingress: V1beta1Ingress):
        '''Create an ingress in a given namespace. Raises an `ApiException` if such an ingress
        already exists.'''
        not_empty(namespace)
        not_none(ingress)

        self.extensions_v1beta1_api.create_namespaced_ingress(namespace=namespace, body=ingress)

    def get_ingress(self, namespace: str, name: str) -> V1beta1Ingress:
        '''Return the `V1beta1Ingress` with the given name in the given namespace, or `None` if
        no such ingress exists.'''
        not_empty(namespace)
        not_empty(name)

        try:
            ingress = self.extensions_v1beta1_api.read_namespaced_ingress(
                name=name,
                namespace=namespace
            )
        except ApiException as ae:
            if ae.status == 404:
                return None
            raise ae
        return ingress


class KubernetesPodHelper(object):
    def __init__(self, core_api: CoreV1Api):
        self.core_api = core_api

    def list_pods(self, namespace: str, label_selector: str='', field_selector: str=''):
        '''Find all pods matching given labels and/or fields in the given namespace'''
        not_empty(namespace)

        try:
            pods = self.core_api.list_namespaced_pod(
                namespace,
                label_selector=label_selector,
                field_selector=field_selector,
        )
        except ApiException as ae:
            if ae.status == 404:
                return None
            raise ae
        return pods

    def execute(
        self,
        name: str,
        namespace: str,
        command:[str],
        container:str='',
        stderr:bool=True,
        stdout:bool=True,
        stdin='Not implemented',
        tty='Not implemented',
    ):
        '''Exec a command on a given pod in a given namespace. Does not support redirection of
        stdin or allocation of a tty.
        '''
        not_empty(name)
        not_empty(namespace)
        not_empty(command)

        if stdin != 'Not implemented' or tty != 'Not implemented':
            raise NotImplementedError

        try:
            response = stream(
                self.core_api.connect_post_namespaced_pod_exec,
                name,
                namespace,
                command=command,
                stderr=stderr,
                stdin=False,
                stdout=stdout,
                tty=False,
            )
        except ApiException as ae:
            if ae.status == 404:
                return None
            raise ae
        return response
