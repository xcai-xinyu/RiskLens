"""UI HTTP boundary; no workflow, database or model dependencies."""
import os

import httpx


class APIError(ValueError):
    pass


class APIClient:
    def __init__(self,base_url=None,transport=None):
        self.base_url=base_url or os.environ.get('RISKLENS_API_URL','http://127.0.0.1:8000')
        self.transport=transport

    def call(self,method,path,*,data=None,params=None,export=False):
        try:
            with httpx.Client(base_url=self.base_url,transport=self.transport,timeout=10,trust_env=False) as client:
                response=client.request(method,path,json=data,params=params)
                if response.status_code>=400:
                    raise APIError(f'API request failed ({response.status_code}). Check the run status or service availability.')
                return response.content if export else response.json()
        except httpx.HTTPError:
            raise APIError('API is unavailable. Start the local API service and retry.') from None
