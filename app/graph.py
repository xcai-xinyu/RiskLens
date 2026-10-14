from langgraph.graph import END, START, StateGraph

from app.nodes import foundation_node
from app.state import RiskState


def build_graph(adapter, settings, deadline):
    graph = StateGraph(RiskState)
    graph.add_node('foundation', foundation_node(adapter, settings, deadline))
    graph.add_edge(START, 'foundation')
    graph.add_edge('foundation', END)
    return graph.compile()
