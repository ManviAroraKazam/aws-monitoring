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

all_instances = logger_mongo_instances + main_mongo_instances + mqtt_instances

# --------------------------------------------------------------------
# Helper: Get instance name
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
        print(f"Could not fetch name for {instance_id}: {e}")
        return instance_id

# --------------------------------------------------------------------
# EC2 Monitoring – CPU + Fully Dynamic Disk
# --------------------------------------------------------------------
def monitor_ec2():
    print(f"\n--- EC2 Monitoring --- [{datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}] ---\n")

    utc_now = datetime.datetime.now(pytz.UTC)
    start = utc_now - datetime.timedelta(hours=6)

    for inst_id in all_instances:
        name = get_instance_name(inst_id)
        print(f"Instance: {name} ({inst_id})")

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
                print(f" CPU Utilization: {cpu:.1f}%", end="")
                if cpu > CPU_THRESHOLD:
                    print("  HIGH CPU")
                    issues.append({"Type": "EC2", "Name": name, "Metric": "CPU", "Status": f"High ({cpu:.1f}%)"})
                else:
                    print("  CPU OK")
            else:
                print(" No CPU data in last 6h")
        except Exception as e:
            print(f" Error fetching CPU: {e}")

        # --- Disk: Fully Dynamic Discovery ---
        try:
            response = cw.list_metrics(
                Namespace="CWAgent",
                MetricName="disk_used_percent"
            )

            instance_disks = []
            for metric in response.get("Metrics", []):
                dims = {d["Name"]: d["Value"] for d in metric["Dimensions"]}
                if dims.get("InstanceId") != inst_id:
                    continue

                path = dims.get("path", "/")
                fstype = dims.get("fstype", "")
                device = dims.get("device", "")

                # Filter out noise
                if any(x in path for x in ["/proc", "/sys", "/dev", "/run", "/boot", "/snap", "/tmp"]) or \
                   fstype in ["tmpfs", "devtmpfs", "overlay", "squashfs", "efivarfs"]:
                    continue

                # Get latest value
                stats = cw.get_metric_statistics(
                    Namespace="CWAgent",
                    MetricName="disk_used_percent",
                    Dimensions=metric["Dimensions"],
                    StartTime=start,
                    EndTime=utc_now,
                    Period=300,
                    Statistics=["Maximum"]
                )
                points = stats.get("Datapoints", [])
                if points:
                    usage = max(p["Maximum"] for p in points)
                    instance_disks.append((path, usage, device or fstype))

            if instance_disks:
                instance_disks.sort(key=lambda x: x[1], reverse=True)
                top_path, top_usage, extra = instance_disks[0]

                print(f" Disk Usage ({top_path}): {top_usage:.1f}%", end="")
                if len(instance_disks) > 1:
                    others = ", ".join([f"{p}:{u:.0f}%" for p, u, _ in instance_disks[1:4]])
                    print(f" | Others → {others}", end="")
                print()

                if top_usage > DISK_THRESHOLD:
                    print("  HIGH DISK")
                    issues.append({
                        "Type": "EC2",
                        "Name": name,
                        "Metric": "Disk",
                        "Status": f"High ({top_path}: {top_usage:.1f}%)"
                    })
                else:
                    print("  Disk OK")
            else:
                print(" No Disk metrics published by CloudWatch Agent")
        except Exception as e:
            print(f" Error fetching disk metrics: {e}")

        print("")  # blank line between instances

# --------------------------------------------------------------------
# Rest of your functions (unchanged, only minor formatting)
# --------------------------------------------------------------------
def monitor_beanstalk():
    print(f"\n--- Elastic Beanstalk Monitoring --- [{datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}] ---\n")
    try:
        envs = eb.describe_environments().get("Environments", [])
        for env in envs:
            name = env.get("EnvironmentName", "unknown")
            if env.get("Status") == "Terminated":
                continue
            health = env.get("Health", "Unknown")
            if health != "Green":
                print(f"Environment {name} unhealthy ({health})")
                issues.append({"Type": "Elastic Beanstalk", "Name": name, "Metric": "Health", "Status": health})
            else:
                print(f"Environment {name} healthy.")
    except Exception as e:
        print(f"Error checking Beanstalk: {e}")

def check_fota_api():
    print(f"\n--- FOTA API Check --- [{datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}] ---\n")
    try:
        r = requests.get("https://fota.kazam.in/time", timeout=10)
        if r.status_code == 200 and r.text.strip().isdigit():
            print("FOTA API healthy.")
        else:
            print("Invalid FOTA response.")
            issues.append({"Type": "FOTA API", "Name": "fota.kazam.in/time", "Metric": "Response", "Status": "Invalid"})
    except Exception as e:
        print(f"FOTA API check failed: {e}")
        issues.append({"Type": "FOTA API", "Name": "fota.kazam.in/time", "Metric": "Exception", "Status": str(e)})

def monitor_mqtt_nodes():
    if not MQTT_USERNAME or not MQTT_PASSWORD:
        print("\nMQTT credentials not set → skipping MQTT dashboard check.\n")
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

        wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, "input[placeholder='Username']"))).send_keys(MQTT_USERNAME)
        driver.find_element(By.CSS_SELECTOR, "input[placeholder='Password']").send_keys(MQTT_PASSWORD)
        wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "button.el-button.el-button--primary"))).click()

        wait.until(EC.element_to_be_clickable((By.XPATH, "//li[normalize-space()='Nodes']"))).click()
        time.sleep(4)

        rows = wait.until(EC.presence_of_all_elements_located(
            (By.CSS_SELECTOR, "div.el-table__body-wrapper tr.el-table__row")
        ))

        data = []
        for row in rows:
            cols = row.find_elements(By.CSS_SELECTOR, ".cell")
            if len(cols) >= 7:
                data.append([cols[0].text.strip(), cols[5].text.strip(), cols[6].text.strip()])

        if data:
            print(tabulate(data, headers=["Node", "Memory", "CPU"], tablefmt="grid"))
        else:
            print("No nodes displayed.")
    except Exception as e:
        print(f"Error scraping MQTT dashboard: {e}")
        issues.append({"Type": "MQTT Dashboard", "Name": "All", "Metric": "Scrape", "Status": f"Failed: {e}"})
    finally:
        if driver:
            try:
                driver.quit()
            except:
                pass

def print_summary():
    print("\n" + "="*60)
    print("               ISSUES SUMMARY")
    print("="*60)
    if not issues:
        print("     NO ISSUES DETECTED – ALL SYSTEMS HEALTHY")
        return

    rows = [[i["Type"], i["Name"], i["Metric"], i["Status"]] for i in issues]
    print(tabulate(rows, headers=["Type", "Name", "Metric", "Status"], tablefmt="grid"))

def send_sns(subject, message):
    try:
        sns.publish(
            TopicArn=SNS_TOPIC_ARN,
            Subject=subject[:100],
            Message=message,
            MessageAttributes={
                "urgency": {
                    "DataType": "String",
                    "StringValue": "high" if issues else "normal"
                }
            }
        )
        print(f"\nSNS notification sent → {subject}")
    except Exception as e:
        print(f"\nFailed to send SNS: {e}")

# --------------------------------------------------------------------
# Main Execution
# --------------------------------------------------------------------
if __name__ == "__main__":
    buf = StringIO()
    original_stdout = sys.stdout
    sys.stdout = buf

    monitor_beanstalk()
    monitor_ec2()
    check_fota_api()
    monitor_mqtt_nodes()
    print_summary()

    sys.stdout = original_stdout
    full_report = buf.getvalue()
    print(full_report)

    real_issues = [i for i in issues if i["Type"] not in ["MQTT Dashboard"]]
    subject = (
        f"AWS Alert - {len(real_issues)} Issue(s) Detected"
        if real_issues else
        "AWS Monitoring – All Good"
    )
    send_sns(subject, full_report)