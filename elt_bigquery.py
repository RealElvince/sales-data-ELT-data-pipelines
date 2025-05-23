from airflow import DAG
from airflow.providers.google.cloud.operators.bigquery import BigQueryInsertJobOperator
from airflow.operators.python import PythonOperator
from airflow.operators.empty import EmptyOperator
from datetime import datetime
import pandas as pd
from faker import Faker
import random
from google.cloud import storage
import io
import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Configuration
PROJECT_ID = os.getenv("PROJECT_ID")
BUCKET_NAME = os.getenv("BUCKET_NAME")
GCS_PATH = os.getenv("GCS_PATH")
BIGQUERY_DATASET = os.getenv("BIGQUERY_DATASET")
BIGQUERY_TABLE = os.getenv("BIGQUERY_TABLE")
AMOUNT_TABLE = os.getenv("AMOUNT_TABLE")
TRANSFORMED_TABLE = os.getenv("TRANSFORMED_TABLE")
PROCEDURE_NAME = os.getenv("PROCEDURE_NAME")

# schema definition
schema_fields = [
                    {"name": "order_id", "type": "INTEGER", "mode": "REQUIRED"},
                    {"name": "customer_name", "type": "STRING", "mode": "REQUIRED"},
                    {"name": "order_amount", "type": "FLOAT", "mode": "REQUIRED"},
                    {"name": "order_date", "type": "DATE", "mode": "REQUIRED"},
                    {"name": "product", "type": "STRING", "mode": "REQUIRED"},
                ]


# Function to generate sales data and upload to GCS
def generate_and_upload_sales_data(bucket_name, gcs_path, num_orders=500):
    fake = Faker()
    data = {
        "order_id": [i for i in range(1, num_orders + 1)],
        "customer_name": [fake.name() for _ in range(num_orders)],
        "order_amount": [round(random.uniform(10.0, 1000.0), 2) for _ in range(num_orders)],
        "order_date": [fake.date_between(start_date='-30d', end_date='today') for _ in range(num_orders)],
        "product": [fake.word() for _ in range(num_orders)],
    }
    
    df = pd.DataFrame(data)

    # Convert to CSV
    csv_buffer = io.StringIO()
    df.to_csv(csv_buffer, index=False)
    csv_data = csv_buffer.getvalue()

    # Upload to GCS
    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(gcs_path)
    blob.upload_from_string(csv_data, content_type='text/csv')
    print(f"Data uploaded to gs://{bucket_name}/{gcs_path}")

# Default arguments
default_args = {
    "start_date": datetime(2024, 11, 18),
    "catchup": False,
}

# DAG definition
with DAG(
    "sales_orders_to_bigquery_with_transformation",
    default_args=default_args,
    schedule_interval=None,  # Trigger manually or as needed
) as dag:
    
    start_task = EmptyOperator(task_id="start")
    end_task = EmptyOperator(task_id="end")

    # Task 2: Generate sales data and upload to GCS
    generate_sales_data = PythonOperator(
        task_id="generate_sales_data",
        python_callable=generate_and_upload_sales_data,
        op_kwargs={
            "bucket_name": BUCKET_NAME,
            "gcs_path": GCS_PATH,
            "num_orders": 500,  # Number of orders to generate
        },
    )

    # Task 3: Load data from GCS to BigQuery
    load_to_bigquery = BigQueryInsertJobOperator(
        task_id="load_to_bigquery",
        configuration={
            "load": {
                "sourceUris": [f"gs://{BUCKET_NAME}/{GCS_PATH}"],
                "destinationTable": {
                    "projectId": PROJECT_ID,
                    "datasetId": BIGQUERY_DATASET,
                    "tableId": BIGQUERY_TABLE,
                },
                "sourceFormat": "CSV",
                "writeDisposition": "WRITE_APPEND",
                "skipLeadingRows": 1,  # Skip CSV header row
                "schema": {
                    "fields": schema_fields,
                },
            }
        },
    )
    #Insert data into sales_orders table
    insert_sales_orders = f"""
                           CREATE OR REPLACE PROCEDURE `{PROJECT_ID}.{BIGQUERY_DATASET}.{PROCEDURE_NAME}`(
                            order_id INT64,
                            customer_name STRING,
                            order_amount FLOAT64,
                            order_date DATE,
                            product STRING
                           )

                           BEGIN
                            INSERT INTO `{PROJECT_ID}.{BIGQUERY_DATASET}.{BIGQUERY_TABLE}`(order_id, customer_name, order_amount, order_date, product)
                            VALUES(order_id, customer_name, order_amount, order_date, product);
                           END
                           """
    


    # Insert data into sales_orders table task
    insert_sales_orders_task = BigQueryInsertJobOperator(
        task_id="insert_sales_orders",
        configuration={
            "query": {
                "query": insert_sales_orders,
                "useLegacySql": False,
            }
        },)
    
    # Call the procedure to insert data
    call_procedure = f""" 
                       CALL `{PROJECT_ID}.{BIGQUERY_DATASET}.{PROCEDURE_NAME}`(234, 'Rick Grimes', 150.00, '2024-11-18', 'Widget A');
                """
    
    # Call the procedure task
    call_procedure_task = BigQueryInsertJobOperator(
        task_id="call_procedure",
        configuration={
            "query": {
                "query": call_procedure,
                "useLegacySql": False,}
        }
    )

    # categorize orders by amount
    transform_bq_qry =f"""
                        SELECT 
                            order_id,
                            customer_name,
                            order_amount,
                            CASE
                                WHEN order_amount < 100 THEN 'Small'
                                WHEN order_amount BETWEEN 100 AND 500 THEN 'Medium'
                                ELSE 'Large'
                            END AS order_category,
                            order_date,
                            product,
                            CURRENT_TIMESTAMP() AS load_timestamp
                        FROM `{PROJECT_ID}.{BIGQUERY_DATASET}.{BIGQUERY_TABLE}`
                    """
    
    # Transform BigQuery Data
    transform_bq_data= BigQueryInsertJobOperator(
        task_id="transform_bigquery_data",
        configuration={
          "query": {
                "query": transform_bq_qry,
                "useLegacySql": False,
                "destinationTable": {
                "projectId": PROJECT_ID,
                "datasetId": BIGQUERY_DATASET,
                "tableId": TRANSFORMED_TABLE,
            },
            "writeDisposition": "WRITE_TRUNCATE",
        }
    }
    )

    # customer and number of orders Transform Bigquery Data
    transform_bq_qry_2 = f"""
                        SELECT
                            customer_name,
                            COUNT(order_id) AS number_of_orders,
                            SUM(order_amount) AS total_spent
                            FROM `{PROJECT_ID}.{BIGQUERY_DATASET}.{BIGQUERY_TABLE}`
                            GROUP BY customer_name
                            ORDER BY customer_name
                          """

 

    # Transform BigQuery Data
    transform_bq_data_2 = BigQueryInsertJobOperator(
          task_id="transform_bigquery_data_2",
         configuration={
           "query": {
                "query": transform_bq_qry_2,
                "useLegacySql": False,
                "destinationTable": {
                "projectId": PROJECT_ID,
                "datasetId": BIGQUERY_DATASET,
                "tableId": AMOUNT_TABLE,
            },
            "writeDisposition": "WRITE_TRUNCATE",
        }
    }
    )

    # Task dependencies
    (
        start_task
        >> generate_sales_data
        >> load_to_bigquery
        >> insert_sales_orders_task
        >> call_procedure_task
        >> transform_bq_data
        >> transform_bq_data_2
        >> end_task

    )