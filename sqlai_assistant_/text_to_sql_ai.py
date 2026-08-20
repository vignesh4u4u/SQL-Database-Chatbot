import json
import os
import re
import pandas as pd
from openai import AsyncOpenAI
from sqlalchemy import create_engine, text, inspect
from sql_database_chatbot.prompt_agents_.pipeline_creator_agent_ import (
    text_to_sql_query_agents,fix_sql_with_llm)
from dotenv import load_dotenv

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL","postgresql://postgres:Vicky%409124@localhost:5432/scm_sample_data")

client = AsyncOpenAI(api_key=OPENAI_API_KEY)

engine = create_engine(
    DATABASE_URL,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True
)


def get_table_info(table_name: str = "sample_scm_kovaion", sample_rows: int = 5) -> str:
    inspector = inspect(engine)
    tables = inspector.get_table_names()
    if table_name not in tables:
        raise ValueError(f"Table '{table_name}' does not exist in the database.")
    columns = inspector.get_columns(table_name)
    if not columns:
        raise ValueError(f"Table '{table_name}' does not contain any columns.")

    column_info = "\n".join(f"- {col['name']} ({col['type']})" for col in columns)

    date_like_cols = [col['name'] for col in columns if 'date' in col['name'].lower() or 'Date' in col['name']]

    extra_date_info = ""
    with engine.connect() as conn:
        sample_query = text(f'SELECT * FROM "{table_name}" LIMIT :sample_rows')
        df_sample = pd.read_sql(sample_query, conn, params={"sample_rows": sample_rows})

        for col in date_like_cols:
            try:
                distinct_q = text(f'''
                    SELECT DISTINCT "{col}" 
                    FROM "{table_name}" 
                    WHERE "{col}" IS NOT NULL 
                    ORDER BY "{col}" DESC 
                    LIMIT 12
                ''')
                df_dates = pd.read_sql(distinct_q, conn)
                if not df_dates.empty:
                    values = ", ".join(df_dates[col].astype(str).tolist())
                    extra_date_info += f"\nDistinct values for {col}: {values}"
            except Exception:
                pass

    if df_sample.empty:
        sample_data = "(No sample records available)"
    else:
        sample_data = df_sample.to_markdown(index=False)

    table_info = f"""
DATABASE TABLE

Table:
{table_name}

AVAILABLE COLUMNS:
{column_info}

SAMPLE DATA (first rows):
{sample_data}

DATE COLUMN VALUES (most recent distinct):
{extra_date_info}
"""
    return table_info.strip(), len(columns)


def clean_generated_sql(sql: str) -> str:
    if not sql:
        raise ValueError("LLM returned empty SQL.")
    sql = sql.strip()
    sql = re.sub(r"^```(?:sql)?\s*", "", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\s*```$", "", sql)
    sql = re.sub(r"^sql\s*", "", sql, flags=re.IGNORECASE)
    sql = sql.rstrip(";").strip()
    if ";" in sql:
        raise ValueError("Multiple SQL statements detected.")
    return sql


def validate_read_only_sql(sql: str) -> str:
    sql = clean_generated_sql(sql)
    if not re.match(r"^(SELECT|WITH)\b", sql, re.IGNORECASE | re.DOTALL):
        raise ValueError("Generated SQL is not a SELECT/WITH query.")
    forbidden = re.compile(
        r"\b(INSERT|UPDATE|DELETE|MERGE|DROP|ALTER|TRUNCATE|CREATE|"
        r"GRANT|REVOKE|VACUUM|CALL|DO|COPY|COMMENT|EXECUTE)\b",
        re.IGNORECASE
    )
    if forbidden.search(sql):
        raise ValueError("Forbidden operation detected.")
    try:
        with engine.connect() as conn:
            conn.execute(text(f"EXPLAIN {sql}"))
    except Exception as e:
        raise ValueError(f"SQL failed EXPLAIN validation: {e}")
    return sql


async def call_llm_for_sql(prompt: str) -> str:
    response = await client.chat.completions.create(
        model="gpt-5.2-2025-12-11",
        messages=[
            {
                "role": "system",
                "content": """
You are a production-grade Enterprise PostgreSQL Text-to-SQL engine.

STRICT RULES:
- The supplied database schema is the ONLY source.
- of truth. Never invent tables or columns.
- Return exactly one read-only PostgreSQL SELECT query.
""".strip()
            },
            {
                "role": "user",
                "content": prompt
            }
        ],
        temperature=0,
        max_completion_tokens=1500
    )

    content = response.choices[0].message.content
    if not content:
        raise ValueError("LLM returned empty SQL.")
    return content.strip()

async def generate_sql(
    user_query: str,
    table_name: str = "sample_scm_kovaion",
    max_retries: int = 3
) -> str:

    table_info, column_count = get_table_info(table_name)

    base_prompt = text_to_sql_query_agents(
        table_info=table_info,
        user_query=user_query,
        column_count=column_count
    )
    current_sql = None
    last_error = None
    for attempt in range(max_retries + 1):
        try:
            if attempt == 0:
                raw_sql = await call_llm_for_sql(base_prompt)
            else:
                raw_sql = fix_sql_with_llm(
                    original_prompt=base_prompt,
                    failed_sql=current_sql or "",
                    database_error=str(last_error)
                )
            current_sql = clean_generated_sql(raw_sql)
            validated_sql = validate_read_only_sql(current_sql)
            print(f"\nSQL validation successful on attempt {attempt + 1}")
            return validated_sql

        except Exception as e:
            last_error = e
            print(
                f"\nSQL attempt {attempt + 1} failed:"
                f"\n{str(e)}"
            )
            if current_sql:
                print("\nFailed SQL:")
                print(current_sql)

            if attempt >= max_retries:
                raise ValueError(
                    f"SQL generation failed after {max_retries + 1} attempts. "
                    f"Last error: {str(last_error)}"
                ) from last_error

    raise ValueError("Unable to generate valid SQL.")


def execute_sql(sql: str) -> pd.DataFrame:
    sql = validate_read_only_sql(sql)
    with engine.connect() as conn:
        result = pd.read_sql(text(sql), conn)
    return result

async def run_query(query):
    try:
        sql_query = await generate_sql(user_query=query, table_name="sample_scm_kovaion")
        print("\nGenerated SQL:")
        print(sql_query)
        final_answer = execute_sql(sql_query)
        result_list = final_answer.to_dict(orient="records")
        print("Records Total:", len(result_list))

        return {
            "success": True,
            "result": result_list,
            "pipeline": sql_query,
            "error": None
        }

    except Exception as e:
        print(f"\nError: {e}\n")
        return {
            "success": False,
            "error_type": "pipeline_execution",
            "result": [],
            "pipeline": sql_query,
            "error": str(e)
        }




# while True:
#     user_query = input("ask : ").strip()
#     if user_query.lower() == "exit":
#         break
#     if not user_query:
#         continue
#     try:
#         sql = generate_sql(user_query=user_query, table_name="sample_scm_kovaion")
#         print("\nGenerated SQL:")
#         print(sql)
#         final_answer = execute_sql(sql)
#         print("\nResult:")
#         result_list = final_answer.to_dict(orient="records")
#         print(result_list)
#         print(len(result_list))
#     except Exception as e:
#         print(f"\nError: {e}\n")
