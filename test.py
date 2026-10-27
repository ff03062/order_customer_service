from langgraph.config import get_stream_writer
from langgraph.graph import StateGraph, START, END

def node(state):
    writer = get_stream_writer()
    writer({"progress": "50%"})
    writer({"progress": "100%"})
    return {}

graph = StateGraph(dict)
graph.add_node("node", node)
graph.add_edge(START, "node")
graph.add_edge("node", END)
app = graph.compile()

for chunk in app.stream({}, stream_mode="custom"):
    print(chunk)