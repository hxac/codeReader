# 单篇讲义生成任务

项目仓库: {{ repo_name }}
项目名: {{ project }}
讲义目录（Write/Edit 仅限）: {{ tutorial_dir }}/
代码永久链接 base: {{ permalink_base }}
当前 HEAD: {{ head }}
动作: {{ action }}

{% if action == "update" and prev_head %}
上次 HEAD（previous_head）: {{ prev_head }}
{% endif %}

## 本讲义规格
- id: {{ lec_id }}
- 文件名: {{ filename }}   ← 写到 {{ tutorial_dir }}/{{ filename }}
- 标题: {{ title }}
- 主题: {{ topic }}
- 应覆盖的最小模块: {{ (minimal_modules | join(", ")) or "（自行规划）" }}
- 关键源码: {{ (source_files | join(", ")) or "（自行定位）" }}
- 依赖讲义: {{ (depends_on | join(", ")) or "无" }}

## 任务
按照 `worker.prompt.md` 的规则，生成这一篇讲义。

要求：
- `new` / `rebuild`：从零写该文件
- `update`：先读现有文件，再结合 `git diff previous_head..current_head` 就地更新
- 只写 `{{ tutorial_dir }}/{{ filename }}` 这一文件
- 完成后，用一句话总结本讲义覆盖的最小模块
