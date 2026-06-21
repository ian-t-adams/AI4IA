"""Azure OpenAI **Responses API** Code Interpreter integration.

Code Interpreter is a built-in tool of the Azure OpenAI Responses API that runs
model-authored Python in a sandboxed, Azure-managed container. It is reached at
``POST {endpoint}/openai/v1/responses`` with
``tools:[{type:"code_interpreter",container:{type:"auto"}}]`` — verified on
Microsoft Learn (the v1 GA surface omits ``api-version``; ``preview`` opts into
latest preview features). This is deliberately a *separate* surface from the APIM
model gateway (which fronts chat completions / the classic Responses path), so it
gets its own thin governed client, mirroring the Content Understanding client.

Imported only when document compute is enabled, so the app and tests run without
any Code Interpreter configuration (tests inject a fake client).
"""
