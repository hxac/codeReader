# 单篇讲义生成任务

项目仓库: {{ repo_name }}
项目名: {{ project }}
讲义目录: {{ tutorial_dir }}/
代码永久链接 base: {{ permalink_base }}
当前 HEAD: {{ head }}
动作: {{ action }}

{% if action == "update" and prev_head %}
上次 HEAD: {{ prev_head }}
{% endif %}

---

{% if outline_prompt %}
## 项目学习路线（大纲方法论）

{{ outline_prompt }}
{% endif %}

{% if outline_content %}
## 完整大纲

```json
{{ outline_content }}
```
{% endif %}

{% if prior_summaries %}
## 前置讲义摘要（你必须基于这些已建立的认知继续，不要重复，要承接）

{% for s in prior_summaries %}
### {{ s.id }} {{ s.title }}

{{ s.summary }}
{% endfor %}
{% else %}
（本讲义是第一篇，无前置摘要。）
{% endif %}

---

## 本讲义规格

- id: {{ lec_id }}
- 文件名: {{ filename }}
- 写入路径: {{ tutorial_dir }}/{{ filename }}
- 标题: {{ title }}
- 学习阶段: {{ level | default("自动判断") }}
- 主题: {{ topic }}
- 学习目标: {{ (learning_goals | default([])) | join("；") }}
- 应覆盖的最小模块: {{ (minimal_modules | join("，")) or "由 worker 根据源码自行规划" }}
- 关键源码: {{ (source_files | join("，")) or "由 worker 自行定位" }}
- 代码实践任务: {{ practice_task | default("由 worker 根据主题和源码自行设计") }}
- 依赖讲义: {{ (depends_on | join("，")) or "无" }}

---

## 任务

请按照 worker prompt 生成这一篇讲义。

要求：

1. 只生成 `{{ tutorial_dir }}/{{ filename }}` 这一个文件。
2. 必须结合真实源码。
3. 必须包含代码实践。
4. 必须使用永久链接引用源码。
5. new/rebuild 模式下从零写入。
6. update 模式下先读取旧文件，再结合 diff 更新。
7. 不要修改源码。
8. 不要写其他文件。

完成后，用一句话总结本讲义覆盖的最小模块。
