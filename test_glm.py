import os
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool

load_dotenv('D:/code/my-projects/vilagent-main/.env')

@tool
def my_tool(x: int):
    '''Multiply by 2'''
    return x * 2

llm = ChatOpenAI(
    model=os.getenv('VILAGENT_GLM_MODEL_NAME', 'glm-4.5-flash'),
    api_key=os.getenv('VILAGENT_GLM_API_KEY'),
    base_url=os.getenv('VILAGENT_GLM_BASE_URL', 'https://open.bigmodel.cn/api/paas/v4/'),
    temperature=0.2,
    max_tokens=2048,
    max_retries=1
).bind_tools([my_tool])

try:
    resp = llm.invoke([
        SystemMessage(content='You are a helpful assistant.'),
        HumanMessage(content='What is 4 times 2? Use the tool.')
    ])
    print(resp.tool_calls)
except Exception as e:
    print('ERROR:', e)
