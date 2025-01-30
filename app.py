import streamlit as st
from groq import Groq
from flask import Flask,request
from gevent.pywsgi import WSGIServer
import sqlite3
import pandas as pd
import os
from dotenv import load_dotenv
load_dotenv()

from agents import SQL_Query_Assistant
conn = sqlite3.connect("company.db")


def read_table():
    employees_df = pd.read_sql_query("SELECT * FROM Employees", conn)
    employees_table = employees_df.to_json(orient="records")

    departments_df = pd.read_sql_query("SELECT * FROM Departments", conn)
    departments_table = departments_df.to_json(orient="records")
    conn.close()
    return employees_table, departments_table

def generate_response(input_text):
    client = Groq(api_key=os.environ.get("GROQ_API_KEY"),)
    chat_completion = client.chat.completions.create( messages=[{"role": "user","content": input_text, }
        ],
        model="llama-3.3-70b-versatile",
    )
    return chat_completion.choices[0].message.content

with st.sidebar:
    groq_api_key = st.text_input("Groq API Key", key="chatbot_api_key", type="password")
    "[Get an Groq API key](https://console.groq.com/keys)"
    "[View the source code](https://github.com/streamlit/llm-examples/blob/main/Chatbot.py)"
    "[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/streamlit/llm-examples?quickstart=1)"


st.title("💬 SQL Query Assistan")
if "messages" not in st.session_state:
    st.session_state["messages"] = [{"role": "assistant", "content": "How can I help you?"}]

for msg in st.session_state.messages:
    st.chat_message(msg["role"]).write(msg["content"])
prompt = st.chat_input("Say something")
if prompt:
    if not groq_api_key:
        st.info("Please add your Groq API key to continue.")
        st.stop()
    client = Groq(api_key=groq_api_key)
    table1, table2 = read_table()
    query = SQL_Query_Assistant(table1,table2,prompt)
    answer = generate_response(query)
    st.session_state["messages"].append({"role": "assistant", "content": answer})
    for msg in st.session_state["messages"]:
        st.chat_message(msg["role"]).write(msg["content"])

def clear_chat_history():
    st.session_state.messages = [{"role": "assistant", "content": "How may I assist you today?"}]

st.sidebar.button('Clear Chat History', on_click=clear_chat_history)
