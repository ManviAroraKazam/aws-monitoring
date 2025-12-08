import boto3
import datetime
import pytz
import requests
import os
from io import StringIO
import sys
from tabulate import tabulate

# --------------------------------------------------------------------
# Config
# --------------------------------------------------------------------
AWS_REGION = os.getenv("AWS_DEFAULT_REGION", "ap-south-1")
SNS_TOPIC_ARN = os.getenv("SNS_TOPIC_ARN")
if not SNS_TOPIC_ARN:
    raise RuntimeError("SNS_TOPIC_ARN not set!")

MQTT_USERNAME = os.getenv("MQTT_USERNAME")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD")

CPU_THRESHOLD = 65
DISK_THRESHOLD = 85

# Skip these broken/suspended EB environments
SKIP_EB = {"kazam-app-backend-env", "kazam-web-frontend"}

eb = boto3.client("elasticbeanstalk", region_name=AWS_REGION)
cw = boto3.client("cloudwatch", region_name=AWS_REGION)
sns = boto3.client("sns", region_name=AWS_REGION)
ec2 = boto3.client("ec2", region_name=AWS_REGION)

issues = []

# Your 9 critical instances
INSTANCES = [
    "i-00e0f35f25480f647", "i-0c88e356ad88357b0", "i-070ed38555e983a39",  # logger mongo
    "i-0fd0bddfa1f458b4b", "i-0b3819ce528f9cd9f", "i-051daf3ab8bc94e62",  # main mongo
    "i-0424fb5cb4e35d2d6", "i-0f06227cd4a2e6e15", "i-0333631e1496b0fd1",  # emqx
]

def get_name(i):
    try:
        resp = ec2.describe_tags(Filters=[{"Name": "resource-id", "Values": [i]}])
        for tag in resp["Tags"]:
            if tag["Key"] == "Name":
                return tag["Value"]
        return i
    except:
        return i

# --------------------------------------------------------------------
# EC2 Monitoring – Fixed for GitHub Actions (no PageSize!)
# --------------------------------------------------------------------
def monitor_ec2():
    print(f"\nEC2 Monitoring — {datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}\n")
    now = datetime.datetime.now(pytz.UTC)
    start = now - datetime.timedelta(hours=6)

    # Get ALL disk_used_percent metrics without PageSize (GH Actions fix)
    all_metrics = []
    token = None
    while True:
        kwargs = {"Namespace": "CWAgent", "MetricName": "disk_used_percent"}
        if token:
            kwargs["NextToken"] = token
        resp = cw.list_metrics(**kwargs)
        all_metrics.extend(resp.get("Metrics", []))
        token = resp.get("NextToken")
        if not token:
            break

    for inst_id in INSTANCES:
        name = get_name(inst_id)
        print(f"Instance: {name} ({inst_id})")

        # CPU
        try:
            resp = cw.get_metric_statistics(
                Namespace="AWS/EC2", MetricName="CPUUtilization",
                Dimensions=[{"Name": "InstanceId", "Value": inst_id}],
                StartTime=start, EndTime=now, Period=300, Statistics=["Maximum"]
            )
            cpu = max([p["Maximum"] for p in resp.get("Datapoints", [])]) if resp.get("Datapoints") else None
            if cpu:
                if cpu > CPU_THRESHOLD:
                    print(f" CPU: {cpu:.1f}% → HIGH CPU")
                    issues.append({"Type": "EC2", "Name": name, "Metric": "CPU", "Status": f"{cpu:.1f}%"})
                else:
                    print(f" CPU: {cpu:.1f}% → CPU OK")
            else:
                print(" CPU: No data")
        except: print(" CPU: Error")

        # Disk – now works 100%
        disks = []
        for m in all_metrics:
            dims = {d["Name"]: d["Value"] for d in m["Dimensions"]}
            if dims.get("InstanceId") != inst_id:
                continue
            path = dims.get("path", "/")
            if any(x in path for x in ["/proc", "/sys", "/dev", "/run", "/boot", "/snap", "/tmp"]):
                continue
            if dims.get("fstype") in ["tmpfs", "devtmpfs", "overlay"]:
                continue

            try:
                stats = cw.get_metric_statistics(
                    Namespace="CWAgent", MetricName="disk_used_percent",
                    Dimensions=m["Dimensions"],
                    StartTime=start, EndTime=now, Period=300, Statistics=["Maximum"]
                )
                if stats.get("Datapoints"):
                    usage = max(p["Maximum"] for p in stats["Datapoints"])
                    disks.append((path, usage))
            except:
                pass

        if disks:
            disks.sort(key=lambda x: x[1], reverse=True)
            path, usage = disks[0]
            others = f" (+{len(disks)-1})" if len(disks) > 1 else ""
            if usage > DISK_THRESHOLD:
                print(f" Disk ({path}): {usage:.1f}%%{others} → HIGH DISK")
                issues.append({"Type": "EC2", "Name": name, "Metric": "Disk", "Status": f"{path}: {usage:.1f}%"})
            else:
                print(f" Disk ({path}): {usage:.1f}%%{others} → Disk OK")
        else:
            print(" Disk: No metrics (agent not configured?)")

        print()

# --------------------------------------------------------------------
# Elastic Beanstalk – Skips suspended ones
# --------------------------------------------------------------------
def monitor_eb():
    print(f"Elastic Beanstalk — {datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}\n")
    try:
        envs = eb.describe_environments()["Environments"]
        for e in envs:
            name = e["EnvironmentName"]
            if name in SKIP_EB:
                print(f"Skipped: {name} (suspended)")
                continue
            health = e.get("Health", "Unknown")
            status = e.get("Status", "")
            if health != "Green" or status in ["Suspended", "Terminating"]:
                print(f"Unhealthy: {name} → {health} ({status})")
                issues.append({"Type": "EB", "Name": name, "Metric": "Health", "Status": f"{health}/{status}"})
            else:
                print(f"Healthy: {name}")
    except Exception as e:
        print(f"EB Error: {e}")

# --------------------------------------------------------------------
# FOTA API
# --------------------------------------------------------------------
def check_fota():
    print(f"\nFOTA API — {datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}\n")
    try:
        r = requests.get("https://fota.kazam.in/time", timeout=10)
        if r.status_code == 200 and r.text.strip().isdigit():
            print("FOTA API Healthy")
        else:
            print("FOTA API Invalid response")
            issues.append({"Type": "FOTA", "Name": "API", "Metric": "Status", "Status": "Invalid"})
    except Exception as e:
        print(f"FOTA API Failed: {e}")
        issues.append({"Type": "FOTA", "Name": "API", "Metric": "Error", "Status": str(e)})

# --------------------------------------------------------------------
# Summary + SNS
# --------------------------------------------------------------------
def summary():
    print("\n" + "═" * 70)
    print(" " * 25 + "ISSUES SUMMARY")
    print("═" * 70)
    if not issues:
        print("   ALL SYSTEMS HEALTHY – NO ISSUES DETECTED!")
    else:
        print(tabulate(
            [[i["Type"], i["Name"], i["Metric"], i["Status"]] for i in issues],
            headers=["Type", "Name", "Metric", "Status"],
            tablefmt="grid"
        ))
    print()

def send_sns():
    subject = f"{'CRITICAL' if issues else 'INFO'} AWS Monitoring – {len(issues)} issue(s)"
    try:
        sns.publish(TopicArn=SNS_TOPIC_ARN, Subject=subject[:100], Message=buf.getvalue())
        print(f"SNS sent → {subject}\n")
    except Exception as e:
        print(f"SNS failed: {e}\n")

# --------------------------------------------------------------------
# Main
# --------------------------------------------------------------------
if __name__ == "__main__":
    buf = StringIO()
    sys.stdout = buf

    monitor_eb()
    monitor_ec2()
    check_fota()
    summary()

    sys.stdout = sys.__stdout__
    print(buf.getvalue())
    send_sns()