import boto3
import datetime
import pytz
import requests
import os
from io import StringIO
import sys
from tabulate import tabulate

# ===================================================================
# CONFIG
# ===================================================================
AWS_REGION = os.getenv("AWS_DEFAULT_REGION", "ap-south-1")
SNS_TOPIC_ARN = os.getenv("SNS_TOPIC_ARN")
if not SNS_TOPIC_ARN:
    raise RuntimeError("SNS_TOPIC_ARN not set!")

# Match CloudWatch Alarm thresholds
CPU_WARN = 70   # Match CloudWatch alarm threshold
CPU_CRIT = 80
DISK_WARN = 85  # Match CloudWatch alarm threshold  
DISK_CRIT = 90

# Monitoring time windows - Match CloudWatch Alarms exactly
CPU_LOOKBACK_MINUTES = 10    # Get 2 periods (2x5min) to ensure we have data
DISK_LOOKBACK_MINUTES = 10   # Get 2 periods (2x5min) to ensure we have data

SKIP_EB = {"kazam-app-backend-env", "kazam-web-frontend"}

eb = boto3.client("elasticbeanstalk", region_name=AWS_REGION)
cw = boto3.client("cloudwatch", region_name=AWS_REGION)
sns = boto3.client("sns", region_name=AWS_REGION)
ec2 = boto3.client("ec2", region_name=AWS_REGION)

issues = []

INSTANCES = [
    "i-00e0f35f25480f647", "i-0c88e356ad88357b0", "i-070ed38555e983a39",
    "i-0fd0bddfa1f458b4b", "i-0b3819ce528f9cd9f", "i-051daf3ab8bc94e62",
    "i-0424fb5cb4e35d2d6", "i-0f06227cd4a2e6e15", "i-0333631e1496b0fd1",
]

def get_name(i):
    try:
        resp = ec2.describe_tags(Filters=[{"Name": "resource-id", "Values": [i]}, {"Name": "key", "Values": ["Name"]}])
        for t in resp["Tags"]:
            if t["Key"] == "Name":
                return t["Value"]
        return i
    except:
        return i

# ===================================================================
# EC2 MONITORING – Matches CloudWatch Alarm Configuration
# ===================================================================
def monitor_ec2():
    print(f"\n🖥️  EC2 Monitoring — {datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}\n")
    now = datetime.datetime.now(pytz.UTC)
    
    # Separate time windows for different metrics
    cpu_start = now - datetime.timedelta(minutes=CPU_LOOKBACK_MINUTES)
    disk_start = now - datetime.timedelta(minutes=DISK_LOOKBACK_MINUTES)

    # Pre-fetch all disk metrics once
    all_metrics = []
    token = None
    while True:
        kwargs = {"Namespace": "CWAgent", "MetricName": "disk_used_percent"}
        if token: kwargs["NextToken"] = token
        resp = cw.list_metrics(**kwargs)
        all_metrics.extend(resp.get("Metrics", []))
        token = resp.get("NextToken")
        if not token: break

    for inst_id in INSTANCES:
        name = get_name(inst_id)
        print(f"Instance: {name} ({inst_id})")

        # CPU - Match CloudWatch Alarm: 5-min period, Average statistic
        try:
            resp = cw.get_metric_statistics(
                Namespace="AWS/EC2", 
                MetricName="CPUUtilization",
                Dimensions=[{"Name": "InstanceId", "Value": inst_id}],
                StartTime=cpu_start, 
                EndTime=now, 
                Period=300,  # 5-minute period (matches CloudWatch alarm)
                Statistics=["Average"]  # Average statistic (matches CloudWatch alarm)
            )
            points = resp.get("Datapoints", [])
            
            if points:
                # Get the most recent 5-minute average (matches CloudWatch behavior)
                sorted_points = sorted(points, key=lambda x: x["Timestamp"], reverse=True)
                cpu = sorted_points[0]["Average"]  # Most recent 5-min average
                
                if cpu >= CPU_CRIT:
                    print(f" 🔴 CPU: {cpu:.1f}% (5-min avg) → CRITICAL CPU")
                    issues.append({
                        "Type": "EC2", 
                        "Name": name, 
                        "Metric": "CPU", 
                        "Value": f"{cpu:.1f}%", 
                        "Status": "CRITICAL CPU"
                    })
                elif cpu >= CPU_WARN:
                    print(f" 🟠 CPU: {cpu:.1f}% (5-min avg) → WARNING CPU")
                    issues.append({
                        "Type": "EC2", 
                        "Name": name, 
                        "Metric": "CPU", 
                        "Value": f"{cpu:.1f}%", 
                        "Status": "WARNING CPU"
                    })
                else:
                    print(f" ✅ CPU: {cpu:.1f}% (5-min avg) → Healthy CPU")
            else:
                print(f" ⚪ CPU: No data in last 10 min")
                issues.append({
                    "Type": "EC2", 
                    "Name": name, 
                    "Metric": "CPU", 
                    "Value": "No data", 
                    "Status": "WARNING CPU"
                })
        except Exception as e:
            print(f" ❌ CPU: Error - {e}")
            issues.append({
                "Type": "EC2", 
                "Name": name, 
                "Metric": "CPU", 
                "Value": "Error", 
                "Status": "WARNING CPU"
            })

        # DISK - Match CloudWatch Alarm: 5-min period, Average statistic
        disks = []
        for m in all_metrics:
            dims = {d["Name"]: d["Value"] for d in m["Dimensions"]}
            if dims.get("InstanceId") != inst_id:
                continue
            
            path = dims.get("path", "")
            device = dims.get("device", "")
            fstype = dims.get("fstype", "")
            
            # Skip system/temporary filesystems
            if any(x in path for x in ["/proc", "/sys", "/dev", "/run", "/boot", "/snap", "/tmp"]):
                continue
            if fstype in ["tmpfs", "devtmpfs", "overlay", "squashfs"]:
                continue
            # Skip loop devices and docker overlays
            if device.startswith("loop") or "overlay" in device.lower():
                continue
            
            # Skip if path is empty
            if not path:
                continue

            try:
                stats = cw.get_metric_statistics(
                    Namespace="CWAgent", 
                    MetricName="disk_used_percent",
                    Dimensions=m["Dimensions"],
                    StartTime=disk_start, 
                    EndTime=now, 
                    Period=300,  # 5-minute period (matches CloudWatch alarm)
                    Statistics=["Average"]  # Average statistic (matches CloudWatch alarm)
                )
                points = stats.get("Datapoints", [])
                if points:
                    # Get the most recent 5-minute average
                    sorted_points = sorted(points, key=lambda x: x["Timestamp"], reverse=True)
                    avg = sorted_points[0]["Average"]
                    
                    # Prefer root filesystem (/) or primary data mounts
                    priority = 0
                    if path == "/":
                        priority = 3  # Highest priority for root
                    elif path.startswith("/data"):
                        priority = 2
                    elif path.startswith("/mnt"):
                        priority = 1
                    
                    disks.append((path, round(avg, 1), device, fstype, priority))
            except:
                pass

        if disks:
            # Sort by priority first (highest first), then by usage (highest first)
            disks.sort(key=lambda x: (x[4], x[1]), reverse=True)
            path, usage, device, fstype, _ = disks[0]
            others = f" (+{len(disks)-1} mounts)" if len(disks) > 1 else ""
            
            # Show device info if available
            device_info = f" [{device}, {fstype}]" if device and fstype else ""
            
            if usage >= DISK_CRIT:
                print(f" 🔴 Disk ({path}): {usage}%{device_info}{others} → CRITICAL DISK")
                issues.append({
                    "Type": "EC2", 
                    "Name": name, 
                    "Metric": "Disk", 
                    "Value": f"{path}: {usage}%{' (' + device + ')' if device else ''}", 
                    "Status": "CRITICAL DISK"
                })
            elif usage >= DISK_WARN:
                print(f" 🟠 Disk ({path}): {usage}%{device_info}{others} → WARNING DISK")
                issues.append({
                    "Type": "EC2", 
                    "Name": name, 
                    "Metric": "Disk", 
                    "Value": f"{path}: {usage}%{' (' + device + ')' if device else ''}", 
                    "Status": "WARNING DISK"
                })
            else:
                print(f" ✅ Disk ({path}): {usage}%{device_info}{others} → Healthy Disk")
        else:
            print(f" ⚪ Disk: No metrics in last 10 min")

        print()

# ===================================================================
# EB + FOTA + PRETTY SUMMARY
# ===================================================================
def monitor_eb():
    print(f"\n🌱 Elastic Beanstalk — {datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}\n")
    try:
        for env in eb.describe_environments()["Environments"]:
            n = env["EnvironmentName"]
            if n in SKIP_EB:
                print(f" ⏸️  Skipped: {n} (suspended)")
                continue
            h = env.get("Health", "Unknown")
            s = env.get("Status", "")
            if h != "Green" or s in ["Suspended", "Terminating"]:
                print(f" ⚠️ Unhealthy: {n} → {h} ({s})")
                issues.append({
                    "Type": "EB", 
                    "Name": n, 
                    "Metric": "Health", 
                    "Value": f"{h}/{s}", 
                    "Status": "Unhealthy EB"
                })
            else:
                print(f" ✅ Healthy: {n}")
    except Exception as e:
        print(f" ❌ EB Error: {e}")

def check_fota():
    print(f"\n🔗 FOTA API Check — {datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}\n")
    try:
        r = requests.get("https://fota.kazam.in/time", timeout=10)
        if r.status_code == 200 and r.text.strip().isdigit():
            print(" ✅ FOTA API Healthy")
        else:
            print(" 🔴 FOTA API Down")
            issues.append({
                "Type": "FOTA", 
                "Name": "API", 
                "Metric": "Status", 
                "Value": "Down", 
                "Status": "FOTA Down"
            })
    except Exception as e:
        print(f" ❌ FOTA Failed: {e}")
        issues.append({
            "Type": "FOTA", 
            "Name": "API", 
            "Metric": "Error", 
            "Value": str(e), 
            "Status": "FOTA Error"
        })

# ===================================================================
# SUMMARY WITH EMOJIS
# ===================================================================
def print_summary():
    print("\n" + "═" * 80)
    print(" " * 30 + "HEALTH SUMMARY")
    print("═" * 80)

    if not issues:
        print(" ✅ ALL SYSTEMS HEALTHY – NO ISSUES!")
        print("═" * 80)
        return

    rows = []
    for i in issues:
        status = i["Status"]
        if "CRITICAL" in status:
            emoji = "🔴"
        elif "WARNING" in status or "Unhealthy" in status or "Down" in status:
            emoji = "🟠"
        else:
            emoji = "✅"
        rows.append([emoji, i["Type"], i["Name"], i["Metric"], i["Value"]])

    print(tabulate(rows, headers=["Status", "Type", "Name", "Metric", "Details"], tablefmt="simple", stralign="left"))
    print("═" * 80)

# ===================================================================
# SNS
# ===================================================================
def send_sns():
    subject = f"{'CRITICAL' if any('CRITICAL' in i['Status'] for i in issues) else 'WARNING' if issues else 'INFO'} AWS Health Check – {len(issues)} issue(s)"
    try:
        sns.publish(TopicArn=SNS_TOPIC_ARN, Subject=subject[:100], Message=buf.getvalue())
        print(f"\n📨 SNS sent → {subject}\n")
    except Exception as e:
        print(f"❌ SNS failed: {e}\n")

# ===================================================================
# MAIN
# ===================================================================
if __name__ == "__main__":
    buf = StringIO()
    sys.stdout = buf

    monitor_eb()
    monitor_ec2()
    check_fota()
    print_summary()

    sys.stdout = sys.__stdout__
    print(buf.getvalue())
    send_sns()