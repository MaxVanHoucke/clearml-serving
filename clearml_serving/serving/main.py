import os
from multiprocessing import Lock
import gzip

from fastapi.middleware.cors import CORSMiddleware
from fastapi import FastAPI, Request, Response, APIRouter, HTTPException
from fastapi.routing import APIRoute

from typing import Optional, Dict, Any, Callable, Union

from clearml_serving.version import __version__
from clearml_serving.serving.init import setup_task
from clearml_serving.serving.model_request_processor import ModelRequestProcessor


class GzipRequest(Request):
    async def body(self) -> bytes:
        if not hasattr(self, "_body"):
            body = await super().body()
            if "gzip" in self.headers.getlist("Content-Encoding"):
                body = gzip.decompress(body)
            self._body = body  # noqa
        return self._body


class GzipRoute(APIRoute):
    def get_route_handler(self) -> Callable:
        original_route_handler = super().get_route_handler()

        async def custom_route_handler(request: Request) -> Response:
            request = GzipRequest(request.scope, request.receive)
            return await original_route_handler(request)

        return custom_route_handler


# process Lock, so that we can have only a single process doing the model reloading at a time
singleton_sync_lock = None  # Lock()
# shared Model processor object
processor = None  # type: Optional[ModelRequestProcessor]

# create clearml Task and load models
serving_service_task_id = setup_task()
# polling frequency
model_sync_frequency_secs = 5
try:
    model_sync_frequency_secs = float(os.environ.get("CLEARML_SERVING_POLL_FREQ", model_sync_frequency_secs))
except (ValueError, TypeError):
    pass


# start FastAPI app
app = FastAPI(title="ClearML Serving Service", version=__version__, description="ClearML Service Service router")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup_event():
    global processor

    if processor:
        print("ModelRequestProcessor already initialized [pid={}] [service_id={}]".format(
            os.getpid(), serving_service_task_id))
    else:
        print("Starting up ModelRequestProcessor [pid={}] [service_id={}]".format(
            os.getpid(), serving_service_task_id))
        processor = ModelRequestProcessor(
            task_id=serving_service_task_id, update_lock_guard=singleton_sync_lock,
        )
        print("ModelRequestProcessor [id={}] loaded".format(processor.get_id()))
        processor.launch(poll_frequency_sec=model_sync_frequency_secs*60)


router = APIRouter(
    prefix="/serve",
    tags=["models"],
    responses={404: {"description": "Model Serving Endpoint Not found"}},
    route_class=GzipRoute,  # mark-out to remove support for GZip content encoding
)


# cover all routing options for model version `/{model_id}`, `/{model_id}/123`, `/{model_id}?version=123`
@router.post("/{model_id}/{version}")
@router.post("/{model_id}/")
@router.post("/{model_id}")
async def serve_model(model_id: str, version: Optional[str] = None, request: Union[bytes, Dict[Any, Any]] = None):
    try:
        return_value = await processor.process_request(
            base_url=model_id,
            version=version,
            request_body=request
        )
    except ValueError as ex:
        raise HTTPException(status_code=404, detail="Error processing request: {}".format(ex))
    except Exception as ex:
        raise HTTPException(status_code=500, detail="Error processing request: {}".format(ex))
    return return_value


app.include_router(router)
