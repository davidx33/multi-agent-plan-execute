from langchain_openai import ChatOpenAI
from langgraph.types import interrupt, Command
import operator
from langgraph.checkpoint.memory import MemorySaver
from langgraph.pregel.remote import RemoteGraph
from langchain_core.messages import SystemMessage, HumanMessage
from langgraph.graph.message import AnyMessage, add_messages
from typing import Annotated
from typing_extensions import TypedDict
from langgraph.graph import END, StateGraph, START
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from typing import Literal, List, Tuple, Union
from langgraph.checkpoint.memory import MemorySaver

load_dotenv()

checkpointer = MemorySaver()

customer_information_deployment_url = "https://react-customer-0485b42e7d885c0fbdad3852a8c0286f.us.langgraph.app"
music_catalog_information_deployment_url = "https://react-music-agent-e21b47f80669524ba69076ff9d720af7.us.langgraph.app"
invoice_information_deployment_url = "https://react-invoice-agent-3913487acf2c5d63ba166fcc35d01641.us.langgraph.app"

customer_information_remote_graph = RemoteGraph("agent", url=customer_information_deployment_url)
music_catalog_information_remote_graph = RemoteGraph("agent", url=music_catalog_information_deployment_url)
invoice_information_remote_graph = RemoteGraph("agent", url=invoice_information_deployment_url)

model = ChatOpenAI(model="o3-mini")

class Config(BaseModel):
    context_for_researcher: list[str]

class State(TypedDict):
    original_objective: str
    action_plan: List[str]
    messages: Annotated[list[AnyMessage], add_messages]
    past_steps: Annotated[List[Tuple], operator.add]
    response: str

class Response(BaseModel):
    """Response to user."""
    response: str

class Step(BaseModel):
    description: str = Field(description="Description of the step to be performed")
    subagent: Literal["customer_information_subagent", "music_catalog_information_subagent", "invoice_information_subagent"] = Field(
        description="Name of the subagent that should execute this step"
    )
    
class Plan(BaseModel):
    steps: List[Step] = Field(
        description="Different steps that the subagents should follow to answer the customer's request, in chronological order"
    )

class PlanWithUserInput(BaseModel):
    steps: List[Step] = Field(
        description="Different steps that the subagents should follow to answer the customer's request, in chronological order"
    )
    updated_objective: str = Field(
        description="The updated objective/request from the customer, after taking into account the user's input/ideas for improvement"
    )

class ReplannerResponse(BaseModel):
    action: Union[Response, Plan] = Field(
        description="The action to perform. If you no longer need to use the subagents to solve the customer's request, use Response. "
        "If you still need to use the subagents to solve the problem, construct and return a list of steps to be performed by the subagents, use Plan."
    )

supervisor_prompt = """You are an expert customer support assistant for a digital music store. You are dedicated to providing exceptional service and ensuring customer queries are answered thoroughly. You have a team of subagents that you can use to help answer queries from customers. Your primary role is to serve as a supervisor/planner for this multi-agent team that helps answer queries from customers. 
The multi-agent team you are supervising is responsible for handling questions related to the digital music store's music catalog (albums, tracks, songs, etc.), information about a customer's account (name, email, phone number, address, etc.), and information about a customer's past purchases or invoices. Your team is composed of three subagents that you can use to help answer the customer's request:

1. customer_information_subagent: this subagent is able to retrieve and update the personal information associated with a customer's account in the database (specifically, viewing or updating a customer's name, address, phone number, or email).
2. music_catalog_information_subagent: this subagent is able to retrieve information about the digital music store's music catalog (albums, tracks, songs, etc.) from the database.
3. invoice_information_subagent: this subagent is able to retrieve information about a customer's past purchases or invoices from the database.

Your role is to create an action plan that the subagents can follow and execute to thoroughly answer the customer's request. Your action plan should specify the exact steps that should be followed by the subagent(s) in order to successfully answer the customer's request, and which subagent should perform each step. Return the action plan as a list of objects, where each object contains the following fields:
- step: a detailed step that should be followed by the subagent(s) in order to successfully answer a portion of the customer's request.
- subagent_to_perform_step: the subagent that should perform the step.

Return the action plan in chronological order, starting with the first step to be performed, and ending with the last step to be performed. You should try your best not to have multiple steps performed by the same subagent. This is inefficient. If you need a subagent to perform a task, try and group it all into one step.

If you do not need a subagent to answer the customer's request, do not include it in the action plan/list of steps. Your goal is to be as efficient as possible, while ensuring that the customer's request is answered thoroughly. Take a deep breath and think carefully before responding. Go forth and make the customer delighted!
"""

# helpers 
def format_steps(steps_list):
    """
    Convert a list of Step objects into a neatly formatted string.
    
    Args:
        steps_list: List of Step objects
        
    Returns:
        A formatted string containing all key information
    """
    formatted_output = "# STEPS ALREADY PERFORMED BY THE SUBAGENTS\n\n"
    
    for i, step in enumerate(steps_list, 1):
        context_on_step = step[0]
        result_of_step = step[1]
        formatted_output += f"## Step {i}\n\n"
        formatted_output += f"### Task Description\n{context_on_step}\n\n"
        formatted_output += f"### Result\nThe following is the response returned after the subagent performed the task above:\n\n"
        # Add the result content
        formatted_output += f"{result_of_step}\n\n"    
        # Add separator between steps
        if i < len(steps_list):
            formatted_output += "---\n\n"
    return formatted_output

def format_action_plan(steps_list):
    """
    Process a list of Step objects and format them into a comprehensive summary string.
    
    Args:
        steps_list: List of Step objects with description and subagent attributes
        
    Returns:
        Formatted string with key information about agent actions
    """
    summary = "# ACTION PLAN FROM PREVIOUS SUPERVISOR/PLANNER\n\n"
    
    for i, step in enumerate(steps_list, 1):
        # Extract information from Step object
        task_description = step.description
        subagent = step.subagent
        
        # Format the action summary
        summary += f"## Action {i}\n\n"
        summary += f"### Agent\n{subagent}\n\n"
        summary += f"### Task\n{task_description}\n\n"
        
        # Add separator between actions except for the last one
        if i < len(steps_list):
            summary += "---\n\n"
    return summary

def supervisor(state: State) -> dict:
    print("\n" + "="*50)
    print("🎯 SUPERVISOR FUNCTION CALLED")
    print("="*50)
    
    first_message = state["messages"][-1]
    structured_model = model.with_structured_output(Plan)
    # Filter out tool messages from conversation history
    filtered_messages = [msg for msg in state["messages"][-3:] 
                         if not (hasattr(msg, 'type') and msg.type in ["tool", "tool_call"])]
    result = structured_model.invoke([
        SystemMessage(content=supervisor_prompt)
    ] + filtered_messages)
    return {
        "action_plan": result.steps,
        "original_objective": first_message.content,
    }

def human_input(state: State) -> dict:
    print("\n" + "="*50)
    print("👤 HUMAN INPUT FUNCTION CALLED")
    print("="*50)
    user_input = interrupt(
        { "agent's plan that the user can edit": state["action_plan"] }
    )
    print(user_input, "got value back from interrupt")
    structured_model = model.with_structured_output(PlanWithUserInput)
    original_objective = state["original_objective"]
    formatted_action_plan = format_action_plan(state["action_plan"])
    if user_input != "":
        print(user_input, "user input detected, plan to edit") 
        replan_with_user_input_prompt = f"""You are an expert customer support assistant for a digital music store. You are dedicated to providing exceptional service and ensuring customer queries are answered thoroughly. You have a team of subagents that you can use to help answer queries from customers. Your primary role is to serve as a supervisor/planner for this multi-agent team that helps answer these queries from customers. 
The multi-agent team you are supervising is responsible for handling questions related to the digital music store's music catalog (albums, tracks, songs, etc.), information about a customer's account (name, email, phone number, address, etc.), and information about a customer's past purchases or invoices. Your team is composed of three subagents that you can use to help answer the customer's request:

1. customer_information_subagent: this subagent is able to retrieve and update the personal information associated with a customer's account in the database (specifically, viewing or updating a customer's name, address, phone number, or email).
2. music_catalog_information_subagent: this subagent is able to retrieve information about the digital music store's music catalog (albums, tracks, songs, etc.) from the database.
3. invoice_information_subagent: this subagent is able to retrieve information about a customer's past purchases or invoices from the database.

Before you, another supervisor/planner has already created an action plan for the subagents to follow. This action plan was then shown to the customer so they could provide feedback and ideas for improvement. 

Your job is to take the customer's feedback and update the action plan and their original request/objective accordingly. Below, I have attached the previous action plan, the customer's original request/objective that the action plan was created for, and the feedback/ideas for improvement from the customer.

Please take this feedback and construct a new action plan/original objective that the subagents can follow to thoroughly answer the customer's request. If the feedback is not relevant or nonsensical, you can ignore it, and keep the original action plan/original objective.

The updated action plan should specify the exact steps that should be followed by the subagent(s) in order to successfully answer the customer's request, and which subagent should perform each step. Return the action plan as a list of objects, where each object contains the following fields:
- step: a detailed step that should be followed by the subagent(s) in order to successfully answer a portion of the customer's request.
- subagent_to_perform_step: the subagent that should perform the step.

The updated objective should be a detailed description of the customer's request/objective, after taking into account the customer's feedback/ideas for improvement.

Return the action plan in chronological order, starting with the first step to be performed, and ending with the last step to be performed. You should try your best not to have multiple steps performed by the same subagent. This is inefficient. If you need a subagent to perform a task, try and group it all into one step.

If you do not need a subagent to answer the customer's request, do not include it in the action plan/list of steps. Your goal is to be as efficient as possible, while ensuring that the customer's request is answered thoroughly and to the best of your ability.

Use the information below to update the action plan and customer's objective based on their feedback/ideas for improvement.

*IMPORTANT INFORMATION BELOW*

Your original objective/request from the customer was this:
{original_objective}

The original action plan constructed by the previous supervisor/planner was this:
{formatted_action_plan}

The customer's feedback/ideas for improvement are as follows:
{user_input}
"""
        result = structured_model.invoke([SystemMessage(content=replan_with_user_input_prompt)])
        print(result.updated_objective, "updated objective from human input")
        return {
            "action_plan": result.steps,
            "original_objective": result.updated_objective,
        }
    else:
        return

def agent_executor(state: State, config: Config) -> dict:
    print("\n" + "="*50)
    print("🤖 AGENT EXECUTOR FUNCTION CALLED")
    print("="*50)
    plan = state["action_plan"]
    print(state["original_objective"], "original objective from state in agent executor")
    print(plan, "plan from state in agent executor")
    total_plan = "\n".join([
        f"{i+1}. {step.subagent} will: {step.description}" 
        for i, step in enumerate(plan)
    ])
    first_task_subagent = plan[0].subagent
    first_task_description = plan[0].description
    first_task_subagent_response = first_task_subagent + " executed logic to answer the following request from the planner/supervisor: " + first_task_description
    task_formatted_prompt = f"""For the following plan: 
    {total_plan}
    You are tasked with executing the first step #1. {first_task_subagent} will: {first_task_description}
    """
    if first_task_subagent == "customer_information_subagent":
        response = customer_information_remote_graph.invoke({"messages": [HumanMessage(content=task_formatted_prompt)]})["messages"]
        final_response = response[-1]['content']
    elif first_task_subagent == "music_catalog_information_subagent":
        response = music_catalog_information_remote_graph.invoke({"messages": [HumanMessage(content=task_formatted_prompt)]})["messages"]
        final_response = response[-1]['content']
    elif first_task_subagent == "invoice_information_subagent":
        response = invoice_information_remote_graph.invoke({"messages": [HumanMessage(content=task_formatted_prompt)]})["messages"]
        final_response = response[-1]['content']

    return {
        "past_steps": [
            (first_task_subagent_response, final_response)
        ]
    }

def replanner(state: State, config: Config) -> dict:
    print("\n" + "="*50)
    print("🔄 REPLANNER FUNCTION CALLED")
    print("="*50)
    # works
    original_objective = state["original_objective"]
    # needs to be cleaned up
    previous_action_plan = state["action_plan"]
    # needs to be cleaned up
    previous_steps = state["past_steps"]
    formatted_action_plan = format_action_plan(previous_action_plan)
    formatted_steps = format_steps(previous_steps)
    replanner_prompt = f"""You are an expert customer support assistant for a digital music store. You are dedicated to providing exceptional service and ensuring customer queries are answered thoroughly. You have a team of subagents that you can use to help answer queries from customers. Your primary role is to serve as a supervisor/planner for this multi-agent team that helps answer these queries from customers. 
The multi-agent team you are supervising is responsible for handling questions related to the digital music store's music catalog (albums, tracks, songs, etc.), information about a customer's account (name, email, phone number, address, etc.), and information about a customer's past purchases or invoices. Your team is composed of three subagents that you can use to help answer the customer's request:

1. customer_information_subagent: this subagent is able to retrieve and update the personal information associated with a customer's account in the database (specifically, viewing or updating a customer's name, address, phone number, or email).
2. music_catalog_information_subagent: this subagent is able to retrieve information about the digital music store's music catalog (albums, tracks, songs, etc.) from the database.
3. invoice_information_subagent: this subagent is able to retrieve information about a customer's past purchases or invoices from the database.

Before you, another supervisor/planner has already created an action plan for the subagents to follow. I've attached the previous action plan, the customer's original request, and the steps that have already been executed by the subagents below. You should take this information and either update the plan, or return Response if you believe the customer's request has been answered thoroughly and you no longer need to use the subagents to solve the problem.

If you decide to update the action plan, your action plan should specify the exact steps that should be followed by the subagent(s) in order to successfully answer the customer's request, and which subagent should perform each step. Return the action plan as a list of objects, where each object contains the following fields:
- step: a detailed step that should be followed by the subagent(s) in order to successfully answer a portion of the customer's request.
- subagent_to_perform_step: the subagent that should perform the step.

Return the action plan in chronological order, starting with the first step to be performed, and ending with the last step to be performed. You should try your best not to have multiple steps performed by the same subagent. This is inefficient. If you need a subagent to perform a task, try and group it all into one step.

If you do not need a subagent to answer the customer's request, do not include it in the action plan/list of steps. Your goal is to be as efficient as possible, while ensuring that the customer's request is answered thoroughly and to the best of your ability.

Use the information below to either update the action plan, or return Response if you believe the customer's request has been answered thoroughly and you no longer need to use the subagents to solve the problem.

*IMPORTANT INFORMATION BELOW*

Your original objective/request from the customer was this:
{original_objective}

The original action plan constructed by the previous supervisor/planner was this:
{formatted_action_plan}

The subagents have already executed the following steps:
{formatted_steps}

If no more steps are needed and you are ready to respond to the customer, use Response. If you still need to use the subagents to solve the problem, construct and return a list of steps to be performed by the subagents using Plan.
"""
    structured_model = model.with_structured_output(ReplannerResponse)
    result = structured_model.invoke([SystemMessage(content=replanner_prompt)])
    if isinstance(result.action, Response):
        return {"response": result.action.response}
    else:
        return {"action_plan": result.action.steps}

def should_end(state: State):
    print("\n" + "="*50)
    print("🎬 SHOULD_END FUNCTION CALLED")
    print("="*50)
    if "response" in state and state["response"]:
        return END
    else:
        return "agent_executor"

builder = StateGraph(State, config_schema=Config)
builder.add_node("supervisor", supervisor)
builder.add_node("agent_executor", agent_executor)
builder.add_node("replanner", replanner)
builder.add_node("human_input", human_input)
builder.add_edge(START, "supervisor")
builder.add_edge("supervisor", "human_input")
builder.add_edge("human_input", "agent_executor")
builder.add_edge("agent_executor", "replanner")
builder.add_conditional_edges(
    "replanner",
    should_end,
    ["agent_executor", END],
)

memory_saver = MemorySaver()

graph = builder.compile(checkpointer=memory_saver)

initial_input = { "messages": [HumanMessage(content="my customer ID is 1. what is my name? also, whats my most recent purchase? and what albums does the catalog have by U2?")] }

thread = {"configurable": {"thread_id": "1"}}
interrupt_info = ""
# Run the graph until the first interruption
for event in graph.stream(initial_input, thread, stream_mode="updates"):
    print(event)
    print("\n")
    if '__interrupt__' in event:
        interrupt_obj = event['__interrupt__'][0]
        interrupt_value = interrupt_obj.value
        plan_to_edit = interrupt_value["agent's plan that the user can edit"]
        
        formatted_plan = "\n".join([
            f"Step {i+1}: {step.description} (using {step.subagent})"
            for i, step in enumerate(plan_to_edit)
        ])
        
        print("\nCurrent plan:")
        print(formatted_plan)
        print("\nIs there anything you would like to change about the plan? If so, please enter your proposed changes. If not, just press enter.")
        user_response = input() 
        if user_response != "":
            interrupt_info = user_response
new_result = graph.invoke(Command(resume=interrupt_info), config=thread)
print(new_result['response'], "new result")