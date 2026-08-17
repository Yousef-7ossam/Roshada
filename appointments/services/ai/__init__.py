"""Server-side AI assistant service.

The assistant used to run entirely inside the Streamlit process: the provider
call, the conversation window and the safety check all happened in the
presentation tier, which meant provider keys lived on the frontend host and the
model could never see the patient's own record without a round-trip.

This package moves the orchestration behind the API, as a pipeline:

    user message
        -> context   (context.py)     who is asking, and what do we know
        -> prompt    (prompts.py)     versioned template + context block
        -> llm       (llm.py)         the provider seam
        -> validation(validation.py)  is the reply safe to show
        -> response  (pipeline.py)    one structured payload

``pipeline.ask`` is the only entry point views should use.

Layering note: ``llm.py`` is the only module the pipeline talks to. The
provider interface underneath it — typed usage, tool calling, uniform retry
and timeouts — lives in :mod:`.providers`, so swapping the company that
answers is a configuration change and never a pipeline change.
"""
from .pipeline import ask, status

__all__ = ["ask", "status"]
