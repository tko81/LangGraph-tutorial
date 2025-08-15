from typing import Annotated
from typing_extensions import TypedDict

from langchain_core.tools import tool
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langchain.chat_models import init_chat_model
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.types import Command, interrupt
from langchain_tavily import TavilySearch

"""
🔍 为什么程序执行完工具后就结束了？

原因分析：
1. ❌ human_assistance 工具调用了 interrupt()，这会暂停图的执行
2. ❌ interrupt() 会抛出 GraphInterrupt 异常来暂停程序
3. ❌ 原代码没有捕获这个异常，所以程序直接退出了
4. ❌ 需要检查图状态并使用 Command(resume=...) 来恢复执行

解决方案：
✅ 检查图状态 (get_state)
✅ 发现中断后提供人类回应
✅ 使用 Command 恢复图的执行
"""

memory = InMemorySaver()

from dotenv import load_dotenv
load_dotenv()

# 构建state schema
class State(TypedDict):
    messages: Annotated[list, add_messages]

graph_builder = StateGraph(State)

# 将 human_assistance 工具添加到 chatbot 中。该工具使用 interrupt 接收来自人类的信息
# 与 Python 的内置 input() 函数类似，在工具中调用 interrupt 将暂停执行。
# 进度基于检查点持久化。因此，如果它使用 Postgres 进行持久化，只要数据库处于活动状态
# 它就可以随时恢复。在这个例子中，它使用内存检查点进行持久化，只要 Python 内核正在运行，就可以随时恢复
@tool
def human_assistance(query: str) -> str:
    """
    Request assistance from a human. 遇到人类求助时，LLM会调用这个工具，query是指打断的问题是什么
    """
    human_response = interrupt({"query": query})
    return human_response["data"]

# 初始化工具
web_search = TavilySearch(max_results=2)
tools = [web_search, human_assistance]

# 初始化模型 绑定工具
llm = init_chat_model("google_genai:gemini-2.0-flash")
llm_with_tools = llm.bind_tools(tools)

# 定义Node
def chatbot(state: State):
    return {"messages": [llm_with_tools.invoke(state["messages"])]}
# 增加Node
graph_builder.add_node("chatbot", chatbot)

# 定义工具Node
tool_node = ToolNode(tools)
# 增加工具Node
graph_builder.add_node("my_tools", tool_node)

# 增加条件边
graph_builder.add_conditional_edges(
    "chatbot",
    tools_condition,    # langgraph自带的工具条件函数返回字符串是写死的，因此下面的映射字典，只能写tools和__end__
    {"tools": "my_tools", "__end__": END} # 如果不需要工具调用 直接结束
)
# 增加普通边
# Any time a tool is called, we return to the chatbot to decide the next step
graph_builder.add_edge("my_tools", "chatbot")
graph_builder.add_edge(START, "chatbot")
#graph = graph_builder.compile()
# 使用提供的检查点编译 graph，该检查点将在 graph 遍历每个 Node 时将 State 作为检查点
graph = graph_builder.compile(checkpointer=memory)


user_input = "I need some expert guidance for building an AI agent. Could you request assistance for me?"
config = {"configurable": {"thread_id": "1"}}

print("=== 第一阶段：运行图直到中断 ===")
try:
    events = graph.stream(
        {"messages": [{"role": "user", "content": user_input}]},
        config,
        stream_mode="values",
    )
    for event in events:
        if "messages" in event:
            event["messages"][-1].pretty_print()
except Exception as e:
    print(f"图执行中断: {e}")

# 检查图的状态
print("\n=== 检查图状态 ===")
snapshot = graph.get_state(config)
print(f"下一个要执行的节点: {snapshot.next}")
print(f"中断信息: {snapshot.interrupts}")

if snapshot.interrupts:
    print(f"中断详情: {snapshot.interrupts[0].value}")
    
    # 模拟人类回应
    print("\n=== 第二阶段：人类回应并恢复执行 ===")
    human_response = (
        "We, the experts are here to help! We'd recommend you check out LangGraph to build your agent."
        " It's much more reliable and extensible than simple autonomous agents."
    )
    
    print(f"人类回应: {human_response}")
    
    # 创建一个 Command 对象，包含恢复数据human_response
    human_command = Command(resume={"data": human_response})
    # 真正执行 Command，启动图的恢复流程 interrupt() 返回 {"data": human_response}
    # interrupt() 返回后会继续执行图，将human_response作为tool message添加到state的message中
    # 接着流转到ChatRobot Node，读取最新的消息，也就是刚才的tool message做回复
    events = graph.stream(human_command, config, stream_mode="values")
    for event in events:
        if "messages" in event:
            event["messages"][-1].pretty_print()
else:
    print("没有发现中断，程序正常结束")
