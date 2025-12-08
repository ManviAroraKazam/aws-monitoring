import boto3
import datetime
import time
import pytz
import requests
import os
from io import StringIO
import sys
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from tabulate import tabulate

# --------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------
AWS_REGION = os.getenv("AWS_DEFAULT_REGION", "ap-south-1")
SNS_TOPIC_ARN = os.getenv("SNS_TOPIC_ARN")
if not SNS_TOPIC_ARN:
    raise RuntimeError("SNS_TOPIC_ARN environment variable not set.")

# Optional MQTT credentials via secrets
MQTT_USERNAME = os.getenv("MQTT_USERNAME")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD")
MQTT_URL = "https://dashboard.mqtt.kazam.in/#/login"

# Thresholds
CPU_THRESHOLD = 65       # %
STORAGE_THRESHOLD = 84   # %

# AWS clients
eb = boto3.client("elasticbeanstalk", region_name=AWS_REGION)
cw = boto3.client("cloudwatch", region_name=AWS_REGION)
ssm = boto3.client("ssm", region_name=AWS_REGION)
sns = boto3.client("sns", region_name=AWS_REGION)

# --------------------------------------------------------------------
# EC2 instances to monitor (replace with your own)
# --------------------------------------------------------------------
logger_mongo_instances = [
    "i-00e0f35f25480f647",
    "i-0c88e356ad88357b0",
    "i-070ed38555e983a39",
]

main_mongo_instances = [
    "i-0fd0bddfa1f458b4b",
    "i-0b3819ce528f9cd9f",
    "i-051daf3ab8bc94e62",
]

issues = []

# --------------------------------------------------------------------
# Functions
# --------------------------------------------------------------------
def get_instance_name(instance_id):
    """Fetch EC2 Name tag"""
    try:
        ec2 = boto3.client("ec2", region_name=AWS_REGION)
        resp = ec2.describe_instances(InstanceIds=[instance_id])
        for res in resp.get("Reservations", []):
            for inst in res.get("Instances", []):
                for tag in inst.get("Tags", []):
                    if tag["Key"] == "Name":
                        return tag["Value"]
        return instance_id
    except Exception as e:
        print(f"⚠ Could not fetch name for {instance_id}: {e}")
        return instance_id


def check_ec2_cpu(instance_id):
    """Check EC2 CPU utilization (past 6 hours)"""
    end = datetime.datetime.now(pytz.UTC)
    start = end - datetime.timedelta(hours=6)
    try:
        data = cw.get_metric_statistics(
            Namespace="AWS/EC2",
            MetricName="CPUUtilization",
            Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
            StartTime=start,
            EndTime=end,
            Period=300,
            Statistics=["Maximum"]
        )
        points = data.get("Datapoints", [])
        if not points:
            return ["EC2", instance_id, "CPU", "No data"]
        cpu = max(p["Maximum"] for p in points)
        if cpu > CPU_THRESHOLD:
            return ["EC2", instance_id, "CPU", f"High ({cpu:.2f}%)"]
    except Exception as e:
        return ["EC2", instance_id, "CPU", f"Error: {e}"]
    return None


def check_storage(instance_id, path="/data"):
    """Check disk usage via SSM"""
    try:
        resp = ssm.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": [f"df -h {path} | awk 'NR==2 {{print $5, $3, $2}}'"]},
        )
        cmd_id = resp["Command"]["CommandId"]
        for _ in range(20):
            out = ssm.get_command_invocation(CommandId=cmd_id, InstanceId=instance_id)
            if out["Status"] in ("Pending", "InProgress", "Delayed"):
                time.sleep(3)
                continue
            if out["Status"] == "Success":
                result = (out.get("StandardOutputContent") or "").strip()
                if result:
                    parts = result.split()
                    if len(parts) == 3:
                        percent = float(parts[0].replace("%", ""))
                        used, total = parts[1], parts[2]
                        if percent > STORAGE_THRESHOLD:
                            return ["EC2", instance_id, "Storage", f"High ({percent:.2f}% used {used}/{total})"]
                break
            break
        return None
    except Exception as e:
        return ["EC2", instance_id, "Storage", f"Error: {e}"]


def monitor_beanstalk():
    """Check Elastic Beanstalk environment health"""
    print("\n--- Elastic Beanstalk Monitoring ---\n")
    try:
        envs = eb.describe_environments().get("Environments", [])
    except Exception as e:
        print(f"⚠ EB describe failed: {e}")
        issues.append({"Type": "Elastic Beanstalk", "Name": "-", "Metric": "Describe", "Status": str(e)})
        return

    for env in envs:
        name = env.get("EnvironmentName", "unknown")
        if env.get("Status") == "Terminated":
            continue
        health = env.get("Health")
        if health != "Green":
            print(f"⚠ Environment {name} unhealthy ({health})")
            issues.append({"Type": "Elastic Beanstalk", "Name": name, "Metric": "Health", "Status": health})
        else:
            print(f"✅ Environment {name} healthy.")


def monitor_ec2():
    """Check EC2 instances CPU + storage"""
    print("\n--- EC2 Monitoring ---\n")

    for inst in logger_mongo_instances:
        name = get_instance_name(inst)
        cpu = check_ec2_cpu(inst)
        if cpu:
            cpu[1] = name
            issues.append(dict(zip(["Type","Name","Metric","Status"], cpu)))
        storage = check_storage(inst, "/")
        if storage:
            storage[1] = name
            issues.append(dict(zip(["Type","Name","Metric","Status"], storage)))

    for inst in main_mongo_instances:
        name = get_instance_name(inst)
        cpu = check_ec2_cpu(inst)
        if cpu:
            cpu[1] = name
            issues.append(dict(zip(["Type","Name","Metric","Status"], cpu)))
        storage = check_storage(inst, "/data")
        if storage:
            storage[1] = name
            issues.append(dict(zip(["Type","Name","Metric","Status"], storage)))


def monitor_mqtt_nodes():
    """Check MQTT dashboard if credentials are available"""
    if not MQTT_USERNAME or not MQTT_PASSWORD:
        print("\n⚠ MQTT credentials not provided; skipping MQTT check.\n")
        return
    print("\n--- MQTT Nodes Status ---\n")
    driver = None
    try:
        options = webdriver.ChromeOptions()
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        driver = webdriver.Chrome(options=options)
        wait = WebDriverWait(driver, 30)
        driver.get(MQTT_URL)

        username_input = wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, "input[placeholder='Username']")))
        password_input = driver.find_element(By.CSS_SELECTOR, "input[placeholder='Password']")
        username_input.send_keys(MQTT_USERNAME)
        password_input.send_keys(MQTT_PASSWORD)
        login_button = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "button.el-button.el-button--primary")))
        login_button.click()

        nodes_tab = wait.until(EC.element_to_be_clickable((By.XPATH, "//li[normalize-space()='Nodes']")))
        nodes_tab.click()
        time.sleep(3)
        rows = wait.until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, "div.el-table__body-wrapper table.el-table__body tr.el-table__row")))

        data = []
        for row in rows:
            cols = row.find_elements(By.CSS_SELECTOR, ".cell")
            if len(cols) >= 7:
                data.append([cols[0].text, cols[5].text, cols[6].text])

        if data:
            print(tabulate(data, headers=["Node", "Memory", "CPU"], tablefmt="grid"))
        else:
            print("⚠ No MQTT nodes found.")
    except Exception as e:
        print(f"⚠ Error monitoring MQTT nodes: {e}")
        issues.append({"Type": "MQTT Node", "Name": "All", "Metric": "Connection", "Status": f"Failed: {e}"})
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


def check_fota_api():
    """Check FOTA API"""
    print("\n--- FOTA API Check ---\n")
    try:
        r = requests.get("https://fota.kazam.in/time", timeout=10)
        if r.status_code == 200 and r.text.strip().isdigit():
            print("✅ FOTA API healthy.")
        else:
            print("❌ Invalid FOTA response.")
            issues.append({"Type": "FOTA API", "Name": "fota.kazam.in/time", "Metric": "Response", "Status": "Invalid"})
    except Exception as e:
        print(f"❌ FOTA API check failed: {e}")
        issues.append({"Type": "FOTA API", "Name": "fota.kazam.in/time", "Metric": "Exception", "Status": str(e)})


def print_summary():
    print("\n--- ⚠ Issues Summary ---\n")
    if not issues:
        print("✅ No issues detected. All systems healthy.\n")
        return
    rows = [[i["Type"], i["Name"], i["Metric"], i["Status"]] for i in issues if i["Type"] != "MQTT Node"]
    print(tabulate(rows, headers=["Type", "Name", "Metric", "Status"], tablefmt="grid"))


def send_sns(subject, message):
    """Publish SNS notification"""
    try:
        resp = sns.publish(
            TopicArn=SNS_TOPIC_ARN,
            Subject=subject,
            Message=message,
            MessageAttributes={
                "urgency": {"DataType": "String", "StringValue": "high" if issues else "normal"}
            },
        )
        print(f"✅ SNS message sent. ID: {resp['MessageId']}")
    except Exception as e:
        print(f"❌ SNS publish failed: {e}")


# --------------------------------------------------------------------
# Main
# --------------------------------------------------------------------
if __name__ == "__main__":
    buf = StringIO()
    sys_stdout = sys.stdout
    sys.stdout = buf

    monitor_beanstalk()
    monitor_ec2()
    check_fota_api()
    monitor_mqtt_nodes()
    print_summary()

    sys.stdout = sys_stdout
    report = buf.getvalue()
    print(report)

    filtered = [i for i in issues if i["Type"] != "MQTT Node"]
    subject = (
        f"⚠️ AWS Monitoring Alert - {len(filtered)} Issues Detected"
        if filtered
        else "✅ AWS Monitoring Report - All Systems Healthy"
    )
    send_sns(subject, report)
