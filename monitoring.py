import boto3
import datetime
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

# Environments to completely SKIP (suspended/broken)
SKIP_EB_ENVIRONMENTS = {
    "kazam-app-backend-env",
    "kazam-web-frontend"
}

# AWS clients
eb = boto3.client("elasticbeanstalk", region_name=AWS_REGION)
cw = boto3.client("cloudwatch", region_name=AWS_REGION)
sns = boto3.client("sns", region_name=AWS_REGION)
ec2_client = boto3.client("ec2", region_name=AWS_REGION)

issues = []

# --------------------------------------------------------------------
# Instances to monitor
# --------------------------------------------------------------------
all_instances = [
    "i-00e0f35f25480f647", "i-0c88e356ad88357b0", "i-070ed38555e983a39",  # logger-mongo
    "i-0fd0bddfa1f458b4b", "i-0b3819ce528f9cd9f", "i-051daf3ab8bc94e62",  # main mongo
    "i-0424fb5cb4e35d2d6", "i-0f06227cd4a2e6e15", "i-0333631e1496b0fd1",  # emqx
]

# --------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------
def get_instance_name(inst_id):
    try:
        resp = ec2_client.describe_instances(InstanceIds=[inst_id])
        for res in resp.get("Reservations", []):
            for inst in res.get("Instances", []):
                for tag in inst.get("Tags", []):
                    if tag["Key"] == "Name":
                        return tag["Value"]
        return inst_id
    except:
        return inst_id

# --------------------------------------------------------------------
# EC2 Monitoring with FULL pagination + Emojis
# --------------------------------------------------------------------
def monitor_ec2():
    print(f"\n--- EC2 Monitoring --- [{datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}] ---\n")
    utc_now = datetime.datetime.now(pytz.UTC)
    start = utc_now - datetime.timedelta(hours=6)

    # Get ALL disk_used_percent metrics with pagination
    def get_all_disk_metrics():
        paginator = cw.get_paginator('list_metrics')
        iterator = paginator.paginate(
            Namespace="CWAgent",
            MetricName="disk_used_percent",
            PaginationConfig={'PageSize': 100}
        )
        metrics = []
        for page in iterator:
            metrics.extend(page.get('Metrics', []))
        return metrics

    all_disk_metrics = get_all_disk_metrics()

    for inst_id in all_instances:
        name = get_instance_name(inst_id)
        print(f"Instance: {name} ({inst_id})")

        # CPU
        try:
            resp = cw.get_metric_statistics(
                Namespace="AWS/EC2", MetricName="CPUUtilization",
                Dimensions=[{"Name": "InstanceId", "Value": inst_id}],
                StartTime=start, EndTime=utc_now, Period=300, Statistics=["Maximum"]
            )
            cpu = max([p["Maximum"] for p in resp.get("Datapoints", [])]) if resp.get("Datapoints") else None
            if cpu is not None:
                status = "HIGH CPU" if cpu > CPU_THRESHOLD else "CPU OK"
                print(f" CPU: {cpu:.1f}% → {status}")
                if cpu > CPU_THRESHOLD:
                    issues.append({"Type": "EC2", "Name": name, "Metric": "CPU", "Status": f"High ({cpu:.1f}%)"})
            else:
                print(" CPU: No data")
        except: print(" CPU: Error")

        # Disk
        instance_disks = []
        for metric in all_disk_metrics:
            dims = {d["Name"]: d["Value"] for d in metric["Dimensions"]}
            if dims.get("InstanceId") != inst_id:
                continue
            path = dims.get("path", "/")
            if any(bad in path for bad in ["/proc", "/sys", "/dev", "/run", "/boot", "/snap", "/tmp"]):
                continue
            if dims.get("fstype") in ["tmpfs", "devtmpfs", "overlay", "squashfs"]:
                continue

            try:
                stats = cw.get_metric_statistics(
                    Namespace="CWAgent", MetricName="disk_used_percent",
                    Dimensions=metric["Dimensions"],
                    StartTime=start, EndTime=utc_now, Period=300, Statistics=["Maximum"]
                )
                if stats.get("Datapoints"):
                    usage = max(p["Maximum"] for p in stats["Datapoints"])
                    instance_disks.append((path, usage))
            except:
                pass

        if instance_disks:
            instance_disks.sort(key=lambda x: x[1], reverse=True)
            path, usage = instance_disks[0]
            status = "HIGH DISK" if usage > DISK_THRESHOLD else "Disk OK"
            others = f" (+{len(instance_disks)-1} more)" if len(instance_disks) > 1 else ""
            print(f" Disk ({path}): {usage:.1f}%{others} → {status}")
            if usage > DISK_THRESHOLD:
                issues.append({"Type": "EC2", "Name": name, "Metric": "Disk", "Status": f"High ({path}: {usage:.1f}%)"})
        else:
            print(" Disk: No metrics found (agent config issue?)")

        print()

# --------------------------------------------------------------------
# Elastic Beanstalk (skip suspended ones)
# --------------------------------------------------------------------
def monitor_beanstalk():
    print(f"\n--- Elastic Beanstalk Monitoring --- [{datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}] ---\n")
    try:
        envs = eb.describe_environments()["Environments"]
        for env in envs:
            name = env["EnvironmentName"]
            if name in SKIP_EB_ENVIRONMENTS:
                print(f"Skipped: {name} (suspended/broken)")
                continue
            health = env.get("Health", "Unknown")
            status = env.get("Status", "")
            if health != "Green" or status in ["Suspended", "Terminating"]:
                print(f"Unhealthy: {name} → {health} ({status})")
                issues.append({"Type": "EB", "Name": name, "Metric": "Health", "Status": f"{health}/{status}"})
            else:
                print(f"Healthy: {name}")
    except Exception as e:
        print(f"EB Error: {e}")

# --------------------------------------------------------------------
# FOTA + MQTT + Summary + SNS
# --------------------------------------------------------------------
def check_fota_api():
    print(f"\n--- FOTA API Check --- [{datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}] ---\n")
    try:
        r = requests.get("https://fota.kazam.in/time", timeout=10)
        if r.status_code == 200 and r.text.strip().isdigit():
            print("FOTA API Healthy")
        else:
            print("FOTA API Invalid response")
            issues.append({"Type": "FOTA", "Name": "API", "Metric": "Response", "Status": "Invalid"})
    except Exception as e:
        print(f"FOTA API Failed: {e}")
        issues.append({"Type": "FOTA", "Name": "API", "Metric": "Error", "Status": str(e)})

def monitor_mqtt_nodes():
    if not (MQTT_USERNAME and MQTT_PASSWORD):
        print("\nSkipped: MQTT check (no credentials)\n")
        return
    # ... [your existing MQTT code with emojis if you want] ...

def print_summary():
    print("\n" + "═" * 70)
    print(" " * 20 + "ISSUES SUMMARY")
    print("═" * 70)
    if not issues:
        print("       ALL SYSTEMS HEALTHY – NO ISSUES!")
        return
    rows = [[i["Type"], i["Name"], i["Metric"], i["Status"]] for i in issues]
    print(tabulate(rows, headers=["Type", "Name", "Metric", "Status"], tablefmt="grid"))
    print()

def send_sns(subject, message):
    try:
        sns.publish(
            TopicArn=SNS_TOPIC_ARN,
            Subject=subject[:100],
            Message=message,
            MessageAttributes={"urgency": {"DataType": "String", "StringValue": "high" if issues else "normal"}}
        )
        print(f"SNS Sent → {subject}\n")
    except Exception as e:
        print(f"SNS Failed: {e}\n")

# --------------------------------------------------------------------
# Main
# --------------------------------------------------------------------
if __name__ == "__main__":
    buf = StringIO()
    old_stdout = sys.stdout
    sys.stdout = buf

    monitor_beanstalk()
    monitor_ec2()
    check_fota_api()
    # monitor_mqtt_nodes()  # uncomment if you want it back
    print_summary()

    sys.stdout = old_stdout
    report = buf.getvalue()
    print(report)

    real_issues = [i for i in issues if i["Type"] != "MQTT"]
    subject = f"{'CRITICAL' if real_issues else 'INFO'} AWS Health – {len(real_issues)} Issue(s)"
    send_sns(subject, report)