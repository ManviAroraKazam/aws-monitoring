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

MQTT_USERNAME = os.getenv("MQTT_USERNAME")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD")
MQTT_URL = "https://dashboard.mqtt.kazam.in/#/login"

CPU_THRESHOLD = 65
MEMORY_THRESHOLD = 80
DISK_THRESHOLD = 85

# AWS clients
eb = boto3.client("elasticbeanstalk", region_name=AWS_REGION)
cw = boto3.client("cloudwatch", region_name=AWS_REGION)
sns = boto3.client("sns", region_name=AWS_REGION)
ec2_client = boto3.client("ec2", region_name=AWS_REGION)

issues = []

# --------------------------------------------------------------------
# EC2 Instances to monitor
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

mqtt_instances = [
    "i-0424fb5cb4e35d2d6",  # kazam-mqtt-emqx-1
    "i-0f06227cd4a2e6e15",  # kazam-mqtt-emqx-2
    "i-0333631e1496b0fd1",  # kazam-mqtt-emqx-3
]

# --------------------------------------------------------------------
# Helper Function
# --------------------------------------------------------------------
def get_instance_name(instance_id):
    try:
        resp = ec2_client.describe_instances(InstanceIds=[instance_id])
        for res in resp.get("Reservations", []):
            for inst in res.get("Instances", []):
                for tag in inst.get("Tags", []):
                    if tag["Key"] == "Name":
                        return tag["Value"]
        return instance_id
    except Exception as e:
        print(f"⚠ Could not fetch name for {instance_id}: {e}")
        return instance_id

# --------------------------------------------------------------------
# EC2 Monitoring (Dynamic CloudWatch Agent detection)
# --------------------------------------------------------------------
def monitor_ec2():
    print(f"\n--- EC2 Monitoring (CloudWatch Agent) --- [{datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}] ---\n")
    all_instances = logger_mongo_instances + main_mongo_instances + mqtt_instances

    for inst_id in all_instances:
        name = get_instance_name(inst_id)
        print(f"Instance: {name} ({inst_id})")

        utc_now = datetime.datetime.now(pytz.UTC)
        start = utc_now - datetime.timedelta(hours=6)

        # --- CPU ---
        try:
            cpu_data = cw.get_metric_statistics(
                Namespace="AWS/EC2",
                MetricName="CPUUtilization",
                Dimensions=[{"Name": "InstanceId", "Value": inst_id}],
                StartTime=start,
                EndTime=utc_now,
                Period=300,
                Statistics=["Maximum"]
            )
            points = cpu_data.get("Datapoints", [])
            if points:
                cpu = max(p["Maximum"] for p in points)
                print(f"  CPU Utilization: {cpu:.1f}%")
                if cpu > CPU_THRESHOLD:
                    issues.append({"Type": "EC2", "Name": name, "Metric": "CPU", "Status": f"High ({cpu:.1f}%)"})
                else:
                    print("  ✅ CPU OK")
            else:
                print("  ⚠ No CPU data available")
        except Exception as e:
            print(f"⚠ Error fetching CPU: {e}")

        # --- Memory (Dynamic Detection) ---
        try:
            mem_metrics = cw.list_metrics(
                Namespace="CWAgent",
                MetricName="mem_used_percent"
            )
            mem_found = False

            for metric in mem_metrics.get("Metrics", []):
                dims = {d["Name"]: d["Value"] for d in metric["Dimensions"]}
                if dims.get("InstanceId") == inst_id:
                    mem_data = cw.get_metric_statistics(
                        Namespace="CWAgent",
                        MetricName="mem_used_percent",
                        Dimensions=metric["Dimensions"],
                        StartTime=start,
                        EndTime=utc_now,
                        Period=300,
                        Statistics=["Maximum"]
                    )
                    points = mem_data.get("Datapoints", [])
                    if points:
                        mem = max(p["Maximum"] for p in points)
                        print(f"  Memory Usage: {mem:.1f}%")
                        if mem > MEMORY_THRESHOLD:
                            issues.append({
                                "Type": "EC2",
                                "Name": name,
                                "Metric": "Memory",
                                "Status": f"High ({mem:.1f}%)"
                            })
                        else:
                            print("  ✅ Memory OK")
                        mem_found = True
                        break

            if not mem_found:
                print("  ⚠ No Memory data available")

        except Exception as e:
            print(f"⚠ Error fetching memory: {e}")

        # --- Disk (Ultra Dynamic Detection) ---
        try:
            disk_metrics = cw.list_metrics(
                Namespace="CWAgent",
                MetricName="disk_used_percent"
            )
            valid_disks = []

            for metric in disk_metrics.get("Metrics", []):
                dims = {d["Name"]: d["Value"] for d in metric["Dimensions"]}
                if dims.get("InstanceId") == inst_id:
                    path = dims.get("path", "")
                    if any(path.startswith(x) for x in ["/proc", "/dev", "/sys", "/run", "/boot", "/snap"]):
                        continue

                    disk_data = cw.get_metric_statistics(
                        Namespace="CWAgent",
                        MetricName="disk_used_percent",
                        Dimensions=metric["Dimensions"],
                        StartTime=start,
                        EndTime=utc_now,
                        Period=300,
                        Statistics=["Maximum"]
                    )
                    points = disk_data.get("Datapoints", [])
                    if points:
                        usage = max(p["Maximum"] for p in points)
                        valid_disks.append((path or "unknown", usage))

            if valid_disks:
                preferred = None
                for path in ["/data", "/", "/mnt"]:
                    for p, val in valid_disks:
                        if p == path:
                            preferred = (p, val)
                            break
                    if preferred:
                        break
                if not preferred:
                    preferred = sorted(valid_disks, key=lambda x: x[1], reverse=True)[0]

                top_path, top_usage = preferred
                print(f"  Disk Usage ({top_path}): {top_usage:.1f}%")
                if top_usage > DISK_THRESHOLD:
                    issues.append({
                        "Type": "EC2",
                        "Name": name,
                        "Metric": "Disk",
                        "Status": f"High ({top_usage:.1f}%)"
                    })
                else:
                    print("  ✅ Disk OK")
            else:
                print("  ⚠ No Disk data available")

        except Exception as e:
            print(f"⚠ Error fetching disk: {e}")

        print("")

# --------------------------------------------------------------------
# Elastic Beanstalk, FOTA, MQTT, and SNS
# --------------------------------------------------------------------
def monitor_beanstalk():
    print(f"\n--- Elastic Beanstalk Monitoring --- [{datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}] ---\n")
    try:
        envs = eb.describe_environments().get("Environments", [])
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
    except Exception as e:
        print(f"⚠ Error: {e}")
        issues.append({"Type": "Elastic Beanstalk", "Name": "-", "Metric": "Error", "Status": str(e)})

def check_fota_api():
    print(f"\n--- FOTA API Check --- [{datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}] ---\n")
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

def monitor_mqtt_nodes():
    if not MQTT_USERNAME or not MQTT_PASSWORD:
        print("\n⚠ MQTT credentials not provided; skipping MQTT check.\n")
        return
    print(f"\n--- MQTT Nodes Status --- [{datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}] ---\n")
    driver = None
    try:
        options = webdriver.ChromeOptions()
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--log-level=3")
        options.add_experimental_option("excludeSwitches", ["enable-logging"])
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
        rows = wait.until(EC.presence_of_all_elements_located(
            (By.CSS_SELECTOR, "div.el-table__body-wrapper table.el-table__body tr.el-table__row")
        ))

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

def print_summary():
    print("\n--- ⚠ Issues Summary ---\n")
    if not issues:
        print("✅ No issues detected. All systems healthy.\n")
        return
    rows = [[i["Type"], i["Name"], i["Metric"], i["Status"]] for i in issues if i["Type"] != "MQTT Node"]
    print(tabulate(rows, headers=["Type", "Name", "Metric", "Status"], tablefmt="grid"))

def send_sns(subject, message):
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