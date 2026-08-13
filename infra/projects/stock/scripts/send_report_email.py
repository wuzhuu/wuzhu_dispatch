#!/usr/bin/env python3
"""发送每日数据报告邮件（独立脚本，避免 heredoc/pipe 问题）"""
import smtplib, sys, yaml, os, glob
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
from email.header import Header
from datetime import datetime

def main():
    if len(sys.argv) < 2:
        print("用法: send_report_email.py <report_file> [target_date]", file=sys.stderr)
        sys.exit(1)

    report_path = sys.argv[1]
    with open(report_path, "r") as f:
        report_text = f.read()
    # 凌晨生成的报告是"昨日"数据，统一措辞
    report_text = report_text.replace("今日", "昨日").replace("今天", "昨日")

    with open(os.path.expanduser("~/key/email_smtp.yaml")) as f:
        cfg = yaml.safe_load(f)["smtp"]

    # 图表/标题日期：优先用传入的 target_date（凌晨生成报告时用上一交易日），
    # 缺省用发送当天（保持向后兼容）。
    if len(sys.argv) >= 3 and sys.argv[2]:
        today = sys.argv[2]
    else:
        today = datetime.now().strftime("%Y-%m-%d")
    charts_dir = os.path.expanduser("~/stock/stock_local_ai_data/artifacts/charts")
    
    # 收集图表
    all_charts = []
    for f in sorted(glob.glob(f"{charts_dir}/{today}_*.png")):
        all_charts.append((False, f))
    for f in sorted(glob.glob(f"{charts_dir}/recommendation/{today}_*.png")):
        all_charts.append((True, f))
    
    print(f"图表: {len(all_charts)} 张")
    
    sender = cfg.get("from_addr", cfg["username"])
    recipient = cfg.get("to_addr", "1521045234@qq.com")

    msg = MIMEMultipart('related')
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = Header(f"【每日数据报告】昨日 {today}", "utf-8")
    
    # 构建 HTML
    html = [
        "<h1 style='margin:0 0 5px 0;font-size:18px;'>股票数据每日分析报告</h1>",
        "<p style='margin:0 0 8px 0;font-size:13px;'><b>交易日（昨日）</b>: %s</p>" % today,
        "<div style='line-height:1.3;'>",
    ]
    for raw_line in report_text.split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("━" * 10):
            continue  # 跳过纯分隔线
        if line.startswith("━━━"):
            title = line.replace("━", "").strip()
            if title:
                html.append("<p style='margin:8px 0 2px 0;font-size:14px;font-weight:bold;color:#333;'>%s</p>" % title)
            continue
        if raw_line.startswith("  "):
            # 缩进行（因子/行业）
            html.append("<p style='margin:0 0 1px 0;padding-left:1.5em;font-size:14px;color:#666;'>%s</p>" % line)
        elif "|" in line and line.count("|") >= 3:
            # 跳过表格分隔线如 |---|---:|
            stripped = line.replace("|", "").replace(":", "").replace("-", "").replace(" ", "").strip()
            if not stripped:
                continue
            # 表格行（Top20），手机避免换行
            html.append("<p style='margin:0 0 1px 0;font-size:12px;font-family:monospace;white-space:nowrap;'>%s</p>" % line)
        else:
            html.append("<p style='margin:0 0 1px 0;font-size:14px;'>%s</p>" % line)
    html.append("</div>")
    
    html.append("<hr>")
    html.append("<h2>基础分析图表</h2>")
    
    rec_header = False
    for is_rec, cf in all_charts:
        fname = os.path.basename(cf)
        readable = fname.replace(f"{today}_", "").replace(".png", "").replace("_", " ")
        if is_rec and not rec_header:
            html.append("<h2>荐股分析图表</h2>")
            rec_header = True
        html.append('<h3>%s</h3><img src="cid:%s" style="max-width:100%%;">' % (readable, fname))
    
    html.append("<hr><p>数据管线自动发送，请勿回复。</p>")
    msg.attach(MIMEText("<br>\n".join(html), "html", "utf-8"))
    
    # 附加图片
    for _, cf in all_charts:
        with open(cf, "rb") as f:
            img = MIMEImage(f.read())
            img.add_header("Content-ID", "<%s>" % os.path.basename(cf))
            img.add_header("Content-Disposition", "inline", filename=os.path.basename(cf))
            msg.attach(img)
    
    # 发送
    try:
        server = smtplib.SMTP(cfg["host"], cfg["port"], timeout=10)
        server.starttls()
        server.login(cfg["username"], cfg["password"])
        server.sendmail(sender, [recipient], msg.as_string())
        server.quit()
        print("邮件发送成功 ✅")
    except Exception as e:
        print(f"邮件发送失败: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
