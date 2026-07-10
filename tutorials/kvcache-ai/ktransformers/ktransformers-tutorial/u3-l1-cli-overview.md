# KT CLI 总览与入口结构

## 1. 本讲目标

本讲带你认识 KTransformers 的命令行工具 `kt`。读完后你应当能够：

- 说清在终端敲下 `kt` 之后，操作系统是如何找到并运行它的（console-script 入点机制）。
- 列出 `kt` 的全部子命令（`version`/`run`/`chat`/`quant`/`edit`/`bench`/`microbench`/`doctor`/`model`/`config`/`sft`），并区分「单命令」与「子应用」两种注册方式。
- 理解「首运行向导」的触发条件与执行流程，知道它如何选语言、发现模型、选存储路径。
- 理解国际化（i18n）的语言探测优先级，以及 shell 补全脚本的静态安装机制。

## 2. 前置知识

在开始前，请确认你已经了解以下概念（不熟悉也没关系，下面会顺带解释）：

- **MoE 与 kt-kernel**：KTransformers 把 MoE 模型的热专家放 GPU、冷专家放 CPU，核心运行时是 `kt-kernel`（见 u1-l1、u1-l2）。`kt` 就是包装这套运行时的命令行外壳。
- **发布名与导入名**：包的发布名是 `kt-kernel`（连字符），但 Python 里 `import` 用的名字是 `kt_kernel`（下划线），见 u1-l3。
- **console-script（控制台脚本）**：Python 打包规范里的一种「入点」（entry point）。你在 `pyproject.toml` 里声明 `kt = "kt_kernel.cli.main:main"`，安装时 setuptools 就会生成一个叫 `kt` 的可执行文件，它内部其实是去调用 `kt_kernel.cli.main` 模块里的 `main()` 函数。这就是为什么 `pip install kt-kernel` 之后直接就能在终端用 `kt` 命令。
- **typer**：一个基于类型注解构建 CLI 的 Python 库，底层是 Click。KTransformers 用它来组织 `kt` 的子命令、参数和帮助文本。
- **TTY**：终端的「电传打字机」抽象。`sys.stdin.isatty()` 用来判断当前进程是不是连着一个真实可交互的终端——这决定了首运行向导要不要弹出来。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `kt-kernel/pyproject.toml` | 声明 `kt` 的 console-script 入点、CLI 依赖（typer/rich 等）与包目录映射。 |
| `kt-kernel/python/cli/main.py` | CLI 主入口：创建 typer app、注册子命令、首运行向导、shell 补全安装、`main()` 调度。 |
| `kt-kernel/python/cli/i18n.py` | 国际化模块：中英文消息表、语言探测与翻译函数 `t()`。 |
| `kt-kernel/python/cli/config/settings.py` | 配置文件管理：`~/.ktransformers/config.yaml` 的默认值与读写。 |
| `kt-kernel/python/cli/commands/config.py` | `kt config` 子应用：`init/show/set/get/reset/path` 等子命令。 |
| `kt-kernel/python/cli/completions/` | 静态 shell 补全脚本（bash/zsh/fish）。 |

> 说明：`pyproject.toml` 里 `[tool.setuptools.package-dir]` 把 `kt_kernel.cli` 映射到 `python/cli`，所以 `kt_kernel.cli.main` 实际指向 `python/cli/main.py`。

## 4. 核心概念与源码讲解

### 4.1 入口与子命令

#### 4.1.1 概念说明

`kt` 不是一个独立的可执行二进制，而是一个 **console-script 入点**：在 `pyproject.toml` 的 `[project.scripts]` 里声明一行，安装时就自动生成一个叫 `kt` 的壳程序，它转发调用到 Python 函数 `kt_kernel.cli.main:main`。

进入 `main()` 后，工作交给 **typer** 框架。typer 提供两种注册子命令的方式：

- **单命令**（`app.command(name=..., help=...)`）：一个函数对应一个顶层子命令，如 `kt version`。
- **子应用**（`app.add_typer(sub_app, name=..., help=...)`）：把另一个 `typer.Typer()` 实例挂上去，形成「命令组」，其下还能再分若干子命令，如 `kt model list`、`kt config show`。

为什么要区分这两者？单命令适合「一个动作」的场景（看版本、跑基准）；子应用适合「一组相关动作」（模型管理有 scan/add/list/edit/verify 等十几个操作）。

#### 4.1.2 核心流程

敲下 `kt` 后的执行链路（伪代码）：

```
终端: kt <args>
  └─ (console-script 壳) → kt_kernel.cli.main:main()
       ├─ _apply_saved_language()        # 先按配置/环境变量定语言
       ├─ _update_help_texts()           # 按当前语言刷新各命令帮助
       ├─ 若 args 为空:
       │    ├─ 是首运行? → 装补全 + 跑首运行向导, 然后 return
       │    └─ 否则      → 打印 --help, return
       ├─ 若 should_check_first_run: 装补全 + check_first_run()
       ├─ 若首参是 "run": 用 click 直接调用 run 命令(透传未知参数)
       └─ app()                          # 交给 typer 分发到具体子命令
```

子命令注册一览：

| 子命令 | 注册方式 | 来源 | 用途 |
| --- | --- | --- | --- |
| `version` | `app.command` | `version.version` | 显示版本信息 |
| `run` | 特殊处理（直接调 click） | `run.run` | 启动推理服务器 |
| `chat` | `app.command` | `chat.chat` | 与运行中模型交互聊天 |
| `quant` | `app.command` | `quant.quant` | 量化模型权重 |
| `edit` | `app.command` | `model.edit_model` | 编辑模型信息 |
| `bench` / `microbench` | `app.command` | `bench.bench` / `bench.microbench` | 完整/微基准测试 |
| `doctor` | `app.command` | `doctor.doctor` | 诊断环境问题 |
| `model` | `app.add_typer` | `model.app` | 管理模型与存储路径（命令组） |
| `config` | `app.add_typer` | `config.app` | 管理配置（命令组） |
| `sft` | `app.add_typer` | `sft.app` | LLaMA-Factory 微调（命令组） |

注意 `run` 没有走普通的 `app.command` 注册，而是在 `main()` 里被单独拦截处理。原因是 `run` 需要把用户传入的**未知参数原样透传**给底层的 `sglang.launch_server`（例如各种 `--kt-*` 参数）。typer/click 默认会对未知参数报错，所以这里用 `click.command(context_settings={"ignore_unknown_options": True, "allow_extra_args": True})` 来放行。

#### 4.1.3 源码精读

入点声明在 `pyproject.toml`：

[kt-kernel/pyproject.toml:L49-L50](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/pyproject.toml#L49-L50) — `[project.scripts]` 下的 `kt = "kt_kernel.cli.main:main"` 就是 `kt` 命令的源头，安装时由 setuptools 生成同名壳程序。

CLI 依赖也在 `pyproject.toml` 里列明，typer 与 rich 是骨架：

[kt-kernel/pyproject.toml:L29-L34](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/pyproject.toml#L29-L34) — typer 负责 CLI 结构，rich 负责彩色表格/面板输出，pyyaml 读写配置，httpx 给 chat/download 用，packaging 做版本比较。

包目录映射，注意发布名 `kt_kernel.cli` 实际指向 `python/cli`：

[kt-kernel/pyproject.toml:L68-L76](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/pyproject.toml#L68-L76) — 这解释了为什么源码在 `python/cli/main.py`，而入点写的是 `kt_kernel.cli.main`。

主入口创建 typer app，注意两个关键开关：

[kt-kernel/python/cli/main.py:L48-L54](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/main.py#L48-L54) — `no_args_is_help=False` 是为了自己接管「无参数」情况（用来触发首运行向导）；`add_completion=False` 是因为本项目改用静态补全脚本，不用 typer 自带的动态补全。

子命令注册集中在文件末尾，单命令用 `app.command`、命令组用 `app.add_typer`：

[kt-kernel/python/cli/main.py:L470-L481](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/main.py#L470-L481) — 这里能一眼看到 `kt` 的全部顶层子命令。注意 `edit` 复用了 `model.edit_model`，`bench` 与 `microbench` 共用 `bench` 模块。

`run` 的特殊拦截，在 `main()` 末段：

[kt-kernel/python/cli/main.py:L534-L541](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/main.py#L534-L541) — 当首参是 `run` 时，绕过 typer 的 `app()`，直接用 `run_module.run.main(args=run_args, standalone_mode=False)` 调用底层 click 命令，从而让未知参数被收进 `ctx.args` 透传给 sglang。

而 `run.py` 命令本身用 click 定义并开启未知参数放行：

[kt-kernel/python/cli/commands/run.py:L34-L38](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/commands/run.py#L34-L38) — `ignore_unknown_options=True` + `allow_extra_args=True` 是透传机制的关键。

#### 4.1.4 代码实践

**实践目标**：亲眼确认 `kt` 的入点链路，并列出全部子命令。

**操作步骤**：

1. 安装 `kt-kernel` 后（见 u2-l1），在终端执行：
   ```bash
   kt --help
   ```
2. 观察输出的「Commands」区段，逐条核对与上表是否一致。
3. 再执行 `which kt`（或 `type kt`），看它指向哪个可执行文件；用文本编辑器打开该文件，确认它内部是去 `from kt_kernel.cli.main import main` 再调用。
4. 执行 `kt version`，确认它能正常输出（哪怕没装 sglang 也应能跑，因为 `version` 在首运行检查的跳过名单里）。

**需要观察的现象**：

- `kt --help` 列出的命令应包含 `version run chat quant edit bench microbench doctor model config sft`。
- `model`/`config`/`sft` 后面会标注它们是命令组（带 SUBCOMMANDS 提示）。
- `which kt` 指向 Python 环境的 `bin/kt`，文件内容是 setuptools 生成的入点壳。

**预期结果**：能完整复述「`kt` → `kt_kernel.cli.main:main` → typer app → 子命令」这条链路。

**待本地验证**：若尚未安装 `kt-kernel`，`kt` 命令不存在；可先 `pip install kt-kernel` 再做本实践。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `run` 不像 `version` 那样用 `app.command` 注册，而要在 `main()` 里单独拦截？

**参考答案**：因为 `run` 需要把任意未知参数（各种 `--kt-*`、sglang 参数）原样透传给底层的 `sglang.launch_server`。typer 默认会对未知参数报错，所以用 `click.command(context_settings={"ignore_unknown_options": True, "allow_extra_args": True})` 定义 `run`，并在 `main()` 里直接以 click 方式调用，绕过 typer 的严格校验。

**练习 2**：`kt edit` 实际调用的是哪个函数？为什么它出现在顶层而不是 `kt model edit` 下？

**参考答案**：调用 `model.edit_model`（见 `main.py:473` 的 `app.command(name="edit", ...)(model.edit_model)`）。它是顶层快捷方式，方便用户少敲一层；`kt model edit` 也能达到同样目的（`model` 子应用里也注册了 `edit`）。

---

### 4.2 首运行向导

#### 4.2.1 概念说明

第一次用 `kt` 时，用户通常既没配语言、也没告诉它模型放在哪。首运行向导（first-run wizard）就是一段交互式引导，依次完成三件事：

1. **选语言**：English 或中文，决定后续所有提示与帮助文本的语言。
2. **发现已有模型**：扫描磁盘上已有的模型权重，方便快速登记进模型列表。
3. **选模型存储位置**：大模型动辄 50–200GB，向导会扫描磁盘容量并推荐空间足够的路径。

向导跑完后，会把结果写进 `~/.ktransformers/config.yaml`，并置 `general._initialized: true` 作为「已完成初始化」的标记，之后不再自动弹出。

#### 4.2.2 核心流程

首运行的判定与执行流程：

```
main()
 ├─ 若 args 为空:
 │    ├─ 配置文件不存在?           → 首运行
 │    └─ 配置存在但 general._initialized != True? → 首运行
 │    若是首运行:
 │       _install_shell_completion()   # 顺带装 shell 补全
 │       check_first_run()             # 跑向导
 │       return
 │    否则: app(["--help"]); return
 │
 └─ check_first_run() 的跳过条件:
      - 不是 TTY (sys.stdin.isatty() == False) → 直接 return (如管道/脚本)
      - args 含 --help/-h/config/version/--version/--no-tui → 跳过

_show_first_run_setup():
  1. 欢迎面板 (中英双语)
  2. 选语言 → settings.set("general.language", lang); set_lang(lang)
  3. 选模型发现方式: 全局扫描 / 手动指定路径 / 跳过
  4. 扫描存储位置 scan_storage_locations(min_size_gb=50)
     → 列出 top 5 + 自定义路径 选项
  5. settings.set("paths.models", 选中路径)
     settings.set("general._initialized", True)
```

关键点：向导**只在没有交互终端时跳过**（避免在 CI/脚本里卡住），并且对 `--help`/`version`/`config` 这类「只想看信息」的命令也跳过，免得打扰。

#### 4.2.3 源码精读

`check_first_run` 判定是否需要弹向导：

[kt-kernel/python/cli/main.py:L77-L101](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/main.py#L77-L101) — 先用 `sys.stdin.isatty()` 挡掉非交互场景；再用配置文件是否存在 + `general._initialized` 标记判断是否首运行。注意它「只检查不创建」——配置文件的真正落盘发生在向导里。

`main()` 对「无参数」情况的接管：

[kt-kernel/python/cli/main.py:L503-L524](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/main.py#L503-L524) — 这里体现了 `no_args_is_help=False` 的用意：无参数时不是直接打帮助，而是先判断首运行，是则跑向导，否则才打 `--help`。

跳过首运行检查的命令名单：

[kt-kernel/python/cli/main.py:L494-L501](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/main.py#L494-L501) — `skip_commands` 里包含 `--help`、`-h`、`config`、`version`、`--version`、`--no-tui`，命中任一则 `should_check_first_run=False`。

向导本体 `_show_first_run_setup`（节选语言选择与存储位置收尾）：

[kt-kernel/python/cli/main.py:L128-L140](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/main.py#L128-L140) — 选 `[1] English` 或 `[2] 中文`，写入 `general.language` 并即时 `set_lang()`。

[kt-kernel/python/cli/main.py:L342-L347](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/main.py#L342-L347) — 向导收尾：写入 `paths.models`，置 `general._initialized=True`，这条 `True` 就是「别再弹向导」的开关。

`general._initialized` 这个标记对应的默认配置定义在 settings 模块：

[kt-kernel/python/cli/config/settings.py:L20-L35](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/config/settings.py#L20-L35) — 默认配置里 `general.language` 为 `"auto"`（交给系统语言探测），`paths.models` 默认指向 `~/.ktransformers/models`。注意默认值里并没有 `_initialized`，它是在向导跑完才被写进去的——所以「字段不存在」与「字段为 False」都视作未初始化。

`kt config init` 子命令其实就是复跑这段向导：

[kt-kernel/python/cli/commands/config.py:L20-L27](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/commands/config.py#L20-L27) — 直接调用 `main._show_first_run_setup(settings)`，无需删配置文件即可重走一遍。

#### 4.2.4 代码实践

**实践目标**：亲手走一遍首运行向导，并验证 `general._initialized` 的写入。

**操作步骤**：

1. 先备份或删除已有配置（若存在）：
   ```bash
   mv ~/.ktransformers/config.yaml ~/.ktransformers/config.yaml.bak 2>/dev/null || true
   ```
2. 执行向导（两种方式任选其一）：
   ```bash
   kt            # 无参数触发首运行
   # 或
   kt config init
   ```
3. 按提示选语言（建议选 `[2] 中文` 体验中文界面），模型发现可选「跳过」，存储位置可接受默认或自选。
4. 向导结束后查看配置：
   ```bash
   kt config show
   # 或直接看文件
   cat ~/.ktransformers/config.yaml
   ```
5. 再次执行 `kt`（无参数），观察它**不再弹向导**，而是直接打印帮助。

**需要观察的现象**：

- 向导中语言选择立刻影响后续提示语言。
- `config.yaml` 里出现 `general._initialized: true` 与 `paths.models: <你选的路径>`。
- 第二次 `kt` 无参数时不再触发向导。

**预期结果**：能解释 `general._initialized` 这个布尔标记如何充当「向导已跑过」的闸门。

**待本地验证**：若在非交互环境（如管道 `echo | kt`）中运行，向导会被 `isatty()` 检查跳过，看不到交互——这是预期行为。

#### 4.2.5 小练习与答案

**练习 1**：如果用户在脚本里用 `kt version`，会不会被首运行向导打断？为什么？

**参考答案**：不会。一方面 `version` 在 `skip_commands` 名单里（`main.py:495`），`should_check_first_run` 会被置 False；另一方面即使没跳过，脚本通常不是 TTY，`check_first_run` 里的 `sys.stdin.isatty()` 也会直接 return。双重保险避免在自动化场景卡住。

**练习 2**：用户改了语言后想重新走一遍存储位置选择，但不删配置文件，该怎么做？

**参考答案**：运行 `kt config init`。它直接调用 `_show_first_run_setup(settings)`，会重走语言/模型发现/存储位置三步，并把 `general._initialized` 重新置 True（见 `config.py:20-27`）。

---

### 4.3 国际化（i18n）与 Shell 补全

#### 4.3.1 概念说明

**国际化（i18n）**：`kt` 面向中英两类用户，所有面向用户的文案都通过一个翻译函数 `t(msg_key)` 取得。`i18n.py` 里维护一张 `MESSAGES` 字典，`en` 和 `zh` 各一份。运行时按当前语言查表，找不到则回退到英文，再找不到就原样返回 key 本身。命令的帮助文本也支持动态切换——`_update_help_texts()` 会在每次启动时按语言重写各命令的 `help` 字段。

**Shell 补全**：让你敲 `kt <Tab>` 时自动补全子命令和选项。本项目**不用** typer 自带的动态补全（`add_completion=False`），而是预置了静态补全脚本（bash/zsh/fish 各一份），首运行时拷贝到 shell 的标准自动加载目录，之后新开终端即生效。静态脚本的好处是补全不依赖 Python 启动，响应快。

#### 4.3.2 核心流程

语言探测的优先级链（`get_lang()`）：

```
1. KT_LANG 环境变量        (最高，用户显式指定，set_lang() 也写它)
2. _lang_cache             (本次进程已探测过的缓存，避免重复 I/O)
3. 配置文件 general.language (非 "auto" 时直接用)
4. 系统 LANG 环境变量      (config 为 "auto" 时按系统语言判断)
5. 默认 English
```

翻译查找（`t()`）：

```
lang = get_lang()
message = MESSAGES[lang].get(key)          # 找不到则
        ↓ 回退
       MESSAGES["en"].get(key)             # 还找不到则
        ↓ 回退
       key 本身
若带 **kwargs: message.format(**kwargs)    # 支持 {name} 占位
```

帮助文本动态切换：

```
main() 启动
  ├─ _apply_saved_language()   # 把配置里的语言喂给 set_lang/get_lang
  └─ _update_help_texts()
       ├─ 改 app.info.help     (主帮助)
       ├─ 遍历 registered_commands 改各命令 help
       └─ 遍历 registered_groups 改各子应用 help
```

Shell 补全安装（`_install_shell_completion()`）：

```
读 general._completion_installed → 已装则 return
检测 $SHELL → bash / zsh / fish
把 cli/completions/ 下对应脚本拷到:
  bash: ~/.local/share/bash-completion/completions/kt
  zsh:  ~/.zfunc/_kt
  fish: ~/.config/fish/completions/kt.fish
置 general._completion_installed = True
```

#### 4.3.3 源码精读

`MESSAGES` 是一张 `{"en": {...}, "zh": {...}}` 的大字典，key 统一、两种语言各一份：

[kt-kernel/python/cli/i18n.py:L12-L13](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/i18n.py#L12-L13) — 所有面向用户的文案都集中在这里，新增语言只需加一个 key。

语言探测 `get_lang()` 实现上述五级优先级：

[kt-kernel/python/cli/i18n.py:L1260-L1303](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/i18n.py#L1260-L1303) — 注意第 2 步的 `_lang_cache` 用来避免每次翻译都去读配置文件；第 3 步从 `settings.get("general.language", "auto")` 读，`"auto"` 表示交给系统 `LANG` 判断。

翻译函数 `t()`，带占位符格式化与回退：

[kt-kernel/python/cli/i18n.py:L1306-L1333](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/i18n.py#L1306-L1333) — 例如 `t("install_found", name="conda", version="24.1.0")` 会把 `"Found {name} (version {version})"` 格式化成完整句子；`KeyError` 时退回原文，避免崩。

`set_lang()` 同时写环境变量和缓存：

[kt-kernel/python/cli/i18n.py:L1336-L1346](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/i18n.py#L1336-L1346) — 它把 `KT_LANG` 设进 `os.environ`，所以同进程内后续 `get_lang()` 第 1 步就能命中。

帮助文本按语言刷新，靠遍历 typer 注册表：

[kt-kernel/python/cli/main.py:L57-L72](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/main.py#L57-L72) — `_update_help_texts()` 用一张 `help_texts` 小表（`main.py:30-44`）把每个命令名映射到中英 help，再写回 `app.registered_commands` / `registered_groups`。

启动时套用已保存语言：

[kt-kernel/python/cli/main.py:L447-L467](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/main.py#L447-L467) — 优先尊重用户已设的 `KT_LANG`（不覆盖），否则读配置 `general.language`（非 auto 才 `set_lang`）。它必须在 `_update_help_texts()` 之前跑，否则帮助还是旧语言。

Shell 补全的静态安装：

[kt-kernel/python/cli/main.py:L391-L444](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/main.py#L391-L444) — 用 `general._completion_installed` 防重复安装；按 `$SHELL` 选目标目录；失败时静默忽略（补全非关键功能）。

补全脚本本身列出了全部主命令：

[kt-kernel/python/cli/completions/kt-completion.bash:L12-L21](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/completions/kt-completion.bash#L12-L21) — 静态脚本把 `commands="version run chat quant edit bench microbench doctor model config sft"` 硬编码，再用 `compgen` 补全。它与 `main.py` 的注册表是两份需要同步维护的清单。

补全脚本通过 `package-data` 打进 wheel：

[kt-kernel/pyproject.toml:L78-L79](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/pyproject.toml#L78-L79) — `*.bash`、`*.fish`、`_kt` 三类文件随包发布，安装后位于 `kt_kernel/cli/completions/`。

#### 4.3.4 代码实践

**实践目标**：验证 i18n 的语言切换，并观察 shell 补全的安装痕迹。

**操作步骤**：

1. 用环境变量临时切换语言（最高优先级，覆盖配置）：
   ```bash
   KT_LANG=zh kt --help
   KT_LANG=en kt --help
   ```
   对比两次输出中主帮助与各命令 help 的语言。
2. 验证回退：用一个故意不存在的 key 看是否原样返回。写一行 Python：
   ```bash
   python -c "from kt_kernel.cli.i18n import t; print(repr(t('not_a_real_key')))"
   ```
3. 检查补全是否已安装（若跑过首运行向导）：
   ```bash
   ls -l ~/.local/share/bash-completion/completions/kt 2>/dev/null
   # 或 zsh: ls -l ~/.zfunc/_kt
   # 或 fish: ls -l ~/.config/fish/completions/kt.fish
   ```
4. 看 `kt config show` 中 `general._completion_installed` 是否为 true。

**需要观察的现象**：

- `KT_LANG=zh` 时帮助变中文，`KT_LANG=en` 时变英文。
- 不存在的 key 原样返回字符串 `'not_a_real_key'`。
- 补全文件存在与否与 `_completion_installed` 一致。

**预期结果**：能复述 `get_lang()` 的五级优先级，并解释为何 `KT_LANG` 优先级最高（用户显式覆盖）。

**待本地验证**：补全效果需在新开的终端里按 Tab 才能体验；当前终端需 `source` 对应补全脚本或重开。

#### 4.3.5 小练习与答案

**练习 1**：用户在 `~/.ktransformers/config.yaml` 里设了 `general.language: zh`，但又执行 `KT_LANG=en kt --help`，帮助是中文还是英文？为什么？

**参考答案**：英文。`get_lang()` 第 1 步先看 `KT_LANG` 环境变量，它是最高优先级；而 `_apply_saved_language()` 也明确「若 `KT_LANG` 已设置则不覆盖」（`main.py:458-459`）。所以环境变量压过配置文件。

**练习 2**：`t("doctor_gpu_found", count=2, names="A100,B100")` 在中文环境下会输出什么？如果 `MESSAGES["zh"]` 里漏了这个 key 会怎样？

**参考答案**：会输出 `"发现 2 个 GPU: A100,B100"`（见 `i18n.py:755` 的中文模板 `"发现 {count} 个 GPU: {names}"`，经 `.format()` 填充）。若 `zh` 漏了该 key，`t()` 会回退到 `MESSAGES["en"]` 的 `"Found {count} GPU(s): {names}"`，仍能正常格式化输出英文文案，不会报错。

**练习 3**：为什么本项目用静态补全脚本而不是 typer 自带的 `add_completion`？

**参考答案**：typer 自带补全每次按 Tab 都要启动 Python 进程动态生成，延迟明显；本项目在 `app = typer.Typer(..., add_completion=False)` 关掉它，改用预置的静态 bash/zsh/fish 脚本（硬编码命令列表），补全时纯 shell 逻辑、不启动 Python，响应快。代价是命令清单要在脚本与 `main.py` 注册表两处同步维护。

## 5. 综合实践

把本讲三个模块串起来，做一次「从入点到向导再到 i18n」的完整追踪：

1. **入点验证**：执行 `which kt` 并打开该文件，找到 `from kt_kernel.cli.main import main`，对应 `pyproject.toml` 的 `[project.scripts]` 声明。
2. **重置并跑向导**：移走 `~/.ktransformers/config.yaml`，执行 `kt`（无参数），在向导里选中文、跳过模型发现、接受默认存储路径。
3. **验证配置落盘**：`kt config show` 确认 `general.language: zh`、`general._initialized: true`、`paths.models` 已写入。
4. **验证 i18n 生效**：执行 `kt --help`，确认帮助为中文；再执行 `KT_LANG=en kt --help`，确认被环境变量覆盖为英文。
5. **验证补全**：检查 `~/.local/share/bash-completion/completions/kt` 是否存在，并在新终端里试 `kt <Tab>`。
6. **写一段小结**：用你自己的话画出「终端 `kt` → 入点 `main()` → 语言套用 → 帮助刷新 → 首运行判定 → typer 分发」的时序，标注每一步对应的源码行号。

> 若本机未装 `kt-kernel`，第 1、5 步的命令可能不存在；可改为纯源码阅读：对照 `main.py:484-543` 的 `main()` 逐行复述调度逻辑。

## 6. 本讲小结

- `kt` 是 console-script 入点，由 `pyproject.toml` 的 `kt = "kt_kernel.cli.main:main"` 生成，调用链终点是 `main.py` 的 `main()`。
- 子命令分两种注册方式：`app.command` 注册单命令（version/chat/quant/edit/bench/microbench/doctor），`app.add_typer` 注册命令组（model/config/sft）；`run` 因需透传未知参数而被单独拦截，用 click 的 `ignore_unknown_options` 实现。
- 首运行向导由 `check_first_run` + `_show_first_run_setup` 实现，靠 `general._initialized` 标记去重，靠 `sys.stdin.isatty()` 与 `skip_commands` 双重避免在非交互/查信息场景打扰用户；`kt config init` 可复跑。
- i18n 由 `MESSAGES` 字典 + `t()` 翻译函数实现，`get_lang()` 按 `KT_LANG` > 缓存 > 配置 > 系统 `LANG` > 默认英文 的五级优先级选语言；`_update_help_texts()` 启动时按语言刷新命令帮助。
- Shell 补全用预置静态脚本（bash/zsh/fish），首运行时由 `_install_shell_completion()` 拷到 shell 自动加载目录，靠 `general._completion_installed` 防重复。

## 7. 下一步学习建议

- 想知道 `kt run` 如何把模型名解析成 sglang 启动命令？继续学 **u3-l2 用 kt run 启动推理服务**，它承接本讲的 `run` 特殊处理，深入 `_build_sglang_command` 与 `--kt-*` 参数。
- 想了解模型注册表与配置文件细节？学 **u3-l3 模型管理与配置**，它展开 `kt model scan/add/list` 与 `kt config show/set`，以及 `~/.ktransformers/config.yaml` 的完整结构。
- 对 `run` 透传给 sglang 的 `--kt-*` 参数语义感兴趣？可先跳读 **u6-l1 SGLang 集成与 kt-* 参数**，再回来看 u3-l2。
- 建议同时用 `kt doctor`（见 `commands/doctor.py`）检查本机环境，为后续真正跑推理做好准备。
