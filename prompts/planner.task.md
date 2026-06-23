# 大纲规划任务

项目仓库: {{ repo_name }}
项目名: {{ project }}
讲义目录: {{ tutorial_dir }}/
模式: {{ mode }}
当前 HEAD: {{ head }}
代码永久链接 base: {{ permalink_base }}
用户关注点: {{ user_focus }}

{% if mode == "incremental" and prev_head %}
上次 HEAD: {{ prev_head }}

## 现有大纲 manifest

```json
{{ existing_manifest }}
```
{% endif %}

---

## 执行要求

请按照 planner prompt 中的规则执行。

你需要输出：

- 一个完整 manifest JSON。
- full 模式下，所有讲义 `action` 为 `"new"`。
- incremental 模式下，根据 diff 判断每篇讲义的 `action`。

不要写任何 Markdown 讲义文件。

不要输出 JSON 以外的任何文字。
