# 大纲规划任务

项目仓库: {{ repo_name }}
项目名: {{ project }}
讲义目录: {{ tutorial_dir }}/
模式: {{ mode }}
当前 HEAD: {{ head }}
代码永久链接 base: {{ permalink_base }}
user_focus: {{ user_focus }}

{% if mode == "incremental" and prev_head %}
上次 HEAD（previous_head）: {{ prev_head }}

## 现有大纲（manifest）
```json
{{ existing_manifest }}
````

{% endif %}

## 任务

根据 `planner.prompt.md` 的规则，输出对应模式下的 manifest JSON。
只输出 JSON，不要输出解释、备注或额外文字。
