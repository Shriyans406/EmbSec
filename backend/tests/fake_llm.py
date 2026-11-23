"""A scripted stand-in for the model, so agent runs can be tested without Gemini.

    FakeLLM([
        call("list_projects"),                        # the model asks for one tool
        call("read_source", project="demo", filename="app.c"),
        say("done: the project has one file"),        # plain answer -> the run ends
    ])

A step may also be a function(messages) -> reply, to answer based on what the tools returned.
"""
import json

from app.ai.llm import BaseLLM, LLMError, LLMReply, ToolCall, Usage

DEFAULT_REPORT = {"outcome": "success", "summary": "scripted run", "facts": ["a fact"], "changes": [],
                  "unverified": [], "next_steps": []}


def call(_tool, **args):
    return {"calls": [(_tool, args)]}


def calls(*pairs):
    return {"calls": list(pairs)}


def say(text):
    return {"text": text}


class FakeLLM(BaseLLM):
    model = "fake-model"

    def __init__(self, script, report=None, tokens_per_call=100):
        super().__init__(None)
        self.script = list(script)
        self.report = report or dict(DEFAULT_REPORT)
        self.tokens = tokens_per_call
        self.seen = []           # (system, messages, tool names) of every generate call
        self.json_seen = []
        self._n = 0

    def _generate(self, system, messages, tools):
        self.seen.append((system, [dict(m) for m in messages], [t["name"] for t in tools or []]))
        if not self.script:
            return self._reply({"text": "(script finished)"})
        step = self.script.pop(0)
        if callable(step):
            step = step(messages)
        if isinstance(step, Exception):
            raise step
        return self._reply(step)

    def _reply(self, step):
        out = []
        for name, args in step.get("calls", []):
            self._n += 1
            out.append(ToolCall(id=f"c{self._n}", name=name, args=args, sig="c2ln"))
        return LLMReply(text=step.get("text", ""), calls=out, usage=Usage(prompt=self.tokens - 10, output=10,
                        total=self.tokens), model=self.model, finish_reason="STOP")

    def _generate_json(self, system, messages, schema):
        self.json_seen.append((system, messages))
        if isinstance(self.report, Exception):
            raise self.report
        return LLMReply(text=json.dumps(self.report), usage=Usage(total=50), model=self.model)


def failing(message="429 RESOURCE_EXHAUSTED"):
    return LLMError(message)
