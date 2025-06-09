import base64
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
from pydantic import ValidationError

from langflow.base.data import BaseFileComponent
from langflow.inputs import IntInput, NestedDictInput, StrInput
from langflow.schema import Data


class DoclingRemoteComponent(BaseFileComponent):
    display_name = "Docling Serve"
    description = "Uses Docling to process input documents connecting to your instance of Docling Serve."
    documentation = "https://docling-project.github.io/docling/"
    trace_type = "tool"
    icon = "Docling"
    name = "DoclingRemote"

    MAX_500_RETRIES = 5

    # https://docling-project.github.io/docling/usage/supported_formats/
    VALID_EXTENSIONS = [
        "adoc",
        "asciidoc",
        "asc",
        "bmp",
        "csv",
        "dotx",
        "dotm",
        "docm",
        "docx",
        "htm",
        "html",
        "jpeg",
        "json",
        "md",
        "pdf",
        "png",
        "potx",
        "ppsx",
        "pptm",
        "potm",
        "ppsm",
        "pptx",
        "tiff",
        "txt",
        "xls",
        "xlsx",
        "xhtml",
        "xml",
        "webp",
    ]

    inputs = [
        *BaseFileComponent._base_inputs,
        StrInput(
            name="api_url",
            display_name="Server address",
            info="URL of the Docling Serve instance.",
            required=True,
        ),
        IntInput(
            name="max_concurrency",
            display_name="Concurrency",
            info="Maximum number of concurrent requests for the server.",
            advanced=True,
            value=2,
        ),
        NestedDictInput(
            name="api_headers",
            display_name="HTTP headers",
            advanced=True,
            required=False,
            info=("Optional dictionary of additional headers required for connecting to Docling Serve."),
        ),
        NestedDictInput(
            name="docling_serve_opts",
            display_name="Docling options",
            advanced=True,
            required=False,
            info=(
                "Optional dictionary of additional options. "
                "See https://github.com/docling-project/docling-serve/blob/main/docs/usage.md for more information."
            ),
        ),
    ]

    outputs = [
        *BaseFileComponent._base_outputs,
    ]

    def process_files(self, file_list: list[BaseFileComponent.BaseFile]) -> list[BaseFileComponent.BaseFile]:
        from docling_core.types.doc import DoclingDocument

        base_url = f"{self.api_url}/v1alpha"

        def _convert_document(client: httpx.Client, file_path: Path, options: dict[str, Any]) -> Data | None:
            encoded_doc = base64.b64encode(file_path.read_bytes()).decode()
            payload = {
                "options": options,
                "file_sources": [{"base64_string": encoded_doc, "filename": file_path.name}],
            }

            response = client.post(f"{base_url}/convert/source/async", json=payload)
            response.raise_for_status()
            task = response.json()

            http_failures = 0
            retry_status_code = 500
            while task["task_status"] not in ("success", "failure"):
                time.sleep(2)
                response = client.get(f"{base_url}/status/poll/{task['task_id']}")
                if response.status_code > retry_status_code:
                    http_failures += 1
                    if http_failures > self.MAX_500_RETRIES:
                        self.log(f"The status requests got a http response {response.status_code} too many times.")
                        return None
                    continue

                task = response.json()

            result_resp = client.get(f"{base_url}/result/{task['task_id']}")
            result_resp.raise_for_status()
            result = result_resp.json()

            if "json_content" not in result["document"] or result["document"]["json_content"] is None:
                self.log("No JSON DoclingDocument found in the result.")
                return None

            try:
                doc = DoclingDocument.model_validate(result["document"]["json_content"])
                return Data(data={"doc": doc, "file_path": str(file_path)})
            except ValidationError as e:
                self.log(f"Error validating the document. {e}")
                return None

        docling_options = {
            "to_formats": ["json"],
            "image_export_mode": "placeholder",
            "return_as_file": False,
            **self.docling_serve_opts,
        }

        processed_data: list[Data | None] = []
        with httpx.Client() as client, ThreadPoolExecutor(max_workers=self.max_concurrency) as executor:
            futures: list[tuple[int, Future]] = []
            for i, file in enumerate(file_list):
                if file.path is None:
                    processed_data.append(None)
                    continue

                futures.append((i, executor.submit(_convert_document, client, file.path, docling_options)))

            for _index, future in futures:
                try:
                    result_data = future.result()
                    processed_data.append(result_data)
                except (
                    httpx.HTTPStatusError,
                    httpx.RequestError,
                    KeyError,
                    ValueError,
                ):
                    self.log("Error occurred")
                    raise

        return self.rollup_data(file_list, processed_data)
