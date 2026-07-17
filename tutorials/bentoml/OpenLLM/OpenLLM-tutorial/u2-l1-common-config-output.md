# 公共基础设施（一）：配置、输出与上下文变量

## 1. 本讲目标

`src/openllm/common.py` 是整个 OpenLLM 的「地基」。前面几讲我们已经见过 `VERBOSE_LEVEL`、`INTERACTIVE`、`output`、`Config` 这些名字被反复引用，但一直没展开。本讲就专门把它们讲透。

学完后你应该能够：

1. 说清楚 `OPENLLM_HOME` 一族路径常量分别指向哪里、何时被创建，以及 `config.json` 何时落盘。
2. 看懂 `Config` 数据模型与 `load_config()` / `save_config()` 的读写流程，能手动读改一份配置。
3. 理解 `ContextVar` 这个「栈式上下文变量」的语义：为什么 `set` 是「压栈」而不是「覆盖」，`patch` 上下文管理器又解决了什么问题。
4. 掌握 `output()` 如何用 `level` 与 `VERBOSE_LEVEL` 配合做「详细度过滤」，并支持把 dict / 对象美化成 YAML 输出。

本讲只聚焦「配置 + 上下文 + 输出」三件套。`common.py` 里的子进程执行（`run_command` / `async_run_command`）留给下一讲 u2-l2，各类业务数据模型（`BentoInfo` / `RepoInfo` / `VenvSpec`）在各自专题讲义里展开。

## 2. 前置知识

在进入源码前，先用大白话过一遍本讲需要的基础概念。

- **模块导入时的副作用（import side effect）**：Python 中写在模块顶层（不在函数里）的语句，会在 `import` 这个模块时立刻执行。`common.py` 把「创建运行时目录」写在了顶层，所以 `import openllm.common` 这一行本身就会在磁盘上建目录。这一点后面会反复用到。
- **Pydantic 数据模型**：`pydantic.BaseModel` 是一个声明式的数据类，你只要写字段名和类型，Pydantic 就帮你做类型校验、默认值、序列化。OpenLLM 用它来描述「配置」「仓库信息」「Bento 信息」等结构化数据。
- **上下文管理器（with 语句）**：`with foo() as x:` 这种写法保证无论代码块是正常结束还是抛异常，都会执行「清理」逻辑。本讲的 `ContextVar.patch` 就是用 `with` 来实现「临时改一个值，离开代码块自动还原」。
- **详细度阈值（verbosity threshold）**：很多 CLI 都有 `--verbose` / `-v` 来控制打印多少信息。OpenLLM 的做法是给每条日志标一个「需要多详细才显示」的等级 `level`，再用一个全局阈值 `VERBOSE_LEVEL` 来卡——只有「消息等级 ≤ 阈值」时才打印。

> 名词约定：本讲里「栈」特指后进先出（LIFO）的数据结构，新放进去的元素在最上面，读取时总是读到最上面那个。

## 3. 本讲源码地图

本讲几乎全部内容都集中在同一个文件里：

| 文件 | 作用 | 本讲关注的部分 |
| --- | --- | --- |
| `src/openllm/common.py` | OpenLLM 全局公共基础设施：路径常量、配置模型、上下文变量、输出函数、子进程执行、各业务数据模型 | 路径常量、`Config`、`load/save_config`、`ContextVar`、`VERBOSE_LEVEL`/`INTERACTIVE`、`output` |
| `src/openllm/__main__.py` | CLI 总入口 | 仅引用它对 `VERBOSE_LEVEL` / `INTERACTIVE` 的真实调用，作为「被使用」的证据 |

一条贯穿全讲的线索是：**`output` 读 `VERBOSE_LEVEL`，`VERBOSE_LEVEL` 由 CLI 全局回调写入，而 `Config` 描述用户持久化的偏好**——三者共同构成 OpenLLM 的「运行时状态层」。

## 4. 核心概念与源码讲解

### 4.1 路径常量与运行时目录（OPENLLM_HOME 家族）

#### 4.1.1 概念说明

OpenLLM 在运行时需要几块「自己的地盘」来放东西：

- **模型仓库的本地克隆**（`repos`）：模型目录本质是 git 仓库，会被克隆到本地。
- **临时文件**（`temp`）：构建、下载过程中的中间产物。
- **每个 Bento 独立的虚拟环境**（`venv`）：因为不同模型依赖不同，OpenLLM 给每个 Bento 单独建一个 venv。
- **用户配置**（`config.json`）：记录用户添加了哪些模型仓库、默认用哪一个。

这些路径不能写死成绝对路径（每个人 home 目录不同），所以 OpenLLM 用一个根目录常量 `OPENLLM_HOME` 来统一派生，默认是 `~/.openllm`，也允许用同名环境变量覆盖。

#### 4.1.2 核心流程

```text
读取环境变量 OPENLLM_HOME
        │
        ├─ 未设置 → 取 ~/.openllm
        └─ 已设置 → 取该值
                │
                ▼
        OPENLLM_HOME（一个 pathlib.Path）
                │
    ┌───────────┼───────────────┬─────────────┐
    ▼           ▼               ▼             ▼
 REPO_DIR    TEMP_DIR        VENV_DIR     CONFIG_FILE
~/openllm  ~/openllm/temp  ~/openllm/venv  ~/openllm/config.json
/repos
```

关键时序：`REPO_DIR / TEMP_DIR / VENV_DIR` 在 **`import openllm.common` 时就被立刻创建**（顶层语句）；而 `CONFIG_FILE`（`config.json`）**只在调用 `save_config()` 时才落盘**，导入时不创建。

#### 4.1.3 源码精读

根目录常量从环境变量读取，缺省回退到用户主目录：

[src/openllm/common.py:16-26](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py#L16-L26) 定义了 `OPENLLM_HOME` 及其派生的三个子目录与配置文件路径。注意 `os.getenv('OPENLLM_HOME', ...)` 的第二参是「取不到时的默认值」。

三个子目录在模块顶层用 `mkdir(exist_ok=True, parents=True)` 创建，所以「导入即建目录」：

[src/openllm/common.py:22-24](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py#L22-L24) 这三行不在任何函数内，`import openllm.common` 时立即执行。`exist_ok=True` 表示目录已存在不报错，`parents=True` 表示连父目录一起建。

而配置文件只是一个「路径常量」，创建动作在 `save_config` 里：

[src/openllm/common.py:26](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py#L26) `CONFIG_FILE` 仅声明路径，本讲 4.2 会看到它何时被真正写入。

#### 4.1.4 代码实践

1. **实践目标**：直观验证「导入即建目录」与 `OPENLLM_HOME` 可被环境变量覆盖。
2. **操作步骤**：
   - 先确认 `~/.openllm` 当前内容（可能还没有）。
   - 用一个临时根目录跑 Python，导入 `openllm.common`：
     ```bash
     OPENLLM_HOME=/tmp/openllm-test python -c "import openllm.common as c; print(c.OPENLLM_HOME)"
     ```
3. **需要观察的现象**：`/tmp/openllm-test` 下应自动出现 `repos`、`temp`、`venv` 三个子目录，但**没有** `config.json`。
4. **预期结果**：打印出 `/tmp/openllm-test`，且 `ls /tmp/openllm-test` 能看到三个目录、看不到 `config.json`——这印证了「目录导入即建、配置按需写」。
5. 如果在你的环境里目录没有出现，标注「待本地验证」并检查是否有写权限。

#### 4.1.5 小练习与答案

- **练习 1**：为什么 `REPO_DIR.mkdir` 用了 `exist_ok=True`？如果去掉会怎样？
  - **答案**：因为 `import openllm.common` 在同一个进程里可能发生多次（多次 import 会命中模块缓存，但跨进程会再次执行顶层语句），而且用户多次运行 `openllm` 命令时目录早已存在。加上 `exist_ok=True` 可让「目录已存在」不再是错误；去掉后第二次运行就会抛 `FileExistsError`。
- **练习 2**：用户想把所有缓存挪到 `/data/openllm`，该怎么做？
  - **答案**：设置环境变量 `OPENLLM_HOME=/data/openllm`（可放进 shell 配置或 systemd unit），因为根目录是从 `os.getenv('OPENLLM_HOME', ...)` 读的。

---

### 4.2 Config 与配置文件读写

#### 4.2.1 概念说明

`Config` 描述的是「用户的持久化偏好」，目前只含两块内容：

- `repos`：一个「仓库名 → 仓库 URL」的字典，记录用户登记了哪些模型仓库。
- `default_repo`：默认使用哪个仓库（对应 `repos` 里的某个键）。

它被序列化成一个简单的 JSON 文件 `config.json`。OpenLLM 在启动时把它读进内存（`load_config`），在 `repo add` / `repo remove` 等操作后把内存里的最新值写回磁盘（`save_config`）。

#### 4.2.2 核心流程

```text
           ┌───────────────┐
           │  config.json  │  （磁盘，可能不存在）
           └───────┬───────┘
        读取  load_config()   写回 save_config(cfg)
                 ▼                    ▲
        ┌────────────────┐   修改    │
        │ Config 对象    │ ────────► │
        │ .repos         │           │
        │ .default_repo  │           │
        └────────────────┘
```

读取的三种情况：

1. 文件**不存在** → 返回默认 `Config()`（带官方 default / nightly 两个仓库）。
2. 文件存在且是**合法 JSON** → 用其内容构造 `Config`。
3. 文件存在但 **JSON 损坏** → 捕获 `json.JSONDecodeError`，降级返回默认 `Config()`（容错，不让用户因坏文件而完全用不了）。

#### 4.2.3 源码精读

`Config` 是一个 Pydantic 模型，`repos` 字段用一个 `default_factory` 来生成默认字典（这样每个实例拿到独立的新字典，而不是共享同一个可变对象）：

[src/openllm/common.py:74-84](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py#L74-L84) 注意默认仓库里 `default` 指向 `@main` 分支、`nightly` 指向 `@nightly` 分支——这就是 u1-l1 提到的「模型仓库本质是 git 仓库」的代码体现。`tolist()` 把模型转成「可被 json 序列化的纯字典」，供写盘用。

读取函数处理「不存在 / 合法 / 损坏」三种情形：

[src/openllm/common.py:87-94](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py#L87-L94) `Config(**json.load(f))` 把 JSON 字典解包成关键字参数传给 Pydantic 模型；`except json.JSONDecodeError` 让损坏文件不至于让整个 CLI 崩溃。

写回函数很简单：用 `tolist()` 拿到纯字典，以 `indent=2` 美化写入：

[src/openllm/common.py:97-99](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py#L97-L99) 因为用 `'w'` 模式打开，每次都是整文件覆盖写，所以 `config.json` 永远反映内存里 `Config` 的最新全量状态。

#### 4.2.4 代码实践

1. **实践目标**：亲手走一遍「读 → 改 → 写 → 再读」的循环，看清 `config.json` 的内容变化。
2. **操作步骤**：在一个**临时** `OPENLLM_HOME`（避免污染你的真实配置）里运行：
   ```python
   import os, openllm.common as c
   # 读：第一次没有 config.json，应得到默认值
   cfg = c.load_config()
   print('读取到:', cfg.tolist())
   # 改：加一个自定义仓库并切换默认
   cfg.repos['myrepo'] = 'https://github.com/me/my-models@main'
   cfg.default_repo = 'myrepo'
   # 写
   c.save_config(cfg)
   print('已写入', c.CONFIG_FILE)
   # 再读：验证落盘内容
   print('重新读取:', c.load_config().tolist())
   ```
3. **需要观察的现象**：第一次打印应是默认的 `default` + `nightly`；写盘后去 `cat` 那个 `config.json`，应看到缩进良好的 JSON，含新增的 `myrepo`；重新读取应与写入一致。
4. **预期结果**：`config.json` 在 `save_config` 之后才出现，内容是 `{"repos": {...}, "default_repo": "myrepo"}`。
5. 想验证「损坏容错」：把 `config.json` 手动改成非法 JSON（如 `{bad`），再跑 `load_config()`，应静默返回默认 `Config()`。

#### 4.2.5 小练习与答案

- **练习 1**：为什么 `repos` 用 `default_factory=lambda: {...}` 而不是直接 `repos: dict = {...}`？
  - **答案**：直接把可变对象（dict）作为默认值会是所有实例共享的同一个对象，A 实例改了会影响 B 实例（Python 经典陷阱）。`default_factory` 让每个新实例都调用一次工厂函数、拿到独立的新字典。
- **练习 2**：`save_config` 用 `'w'` 覆盖写有什么好处和风险？
  - **答案**：好处是实现简单、文件始终是全量最新状态，不会残留旧字段；风险是写盘中途若进程被杀，文件可能损坏（这里靠 `load_config` 的 `JSONDecodeError` 容错兜底）。

---

### 4.3 ContextVar：栈式上下文变量

#### 4.3.1 概念说明

很多行为需要「在程序运行中临时改变，过后还原」。比如 `--verbose` 把日志详细度临时调高，某个交互命令把交互开关临时打开。OpenLLM 没有直接用一个全局变量去赋值，而是自己实现了一个 `ContextVar` 类，特点是**用栈来管理值**：

- `get()`：读栈顶；栈空则读默认值。
- `set(v)`：把 `v` **压入栈顶**（注意：是压栈，不是覆盖！）。
- `patch(v)`：一个 `with` 上下文管理器，进入时压栈，离开时（无论是否异常）弹栈。

这套语义特别适合「嵌套调用」：外层 `patch(10)`、内层再 `patch(20)`，内层读到 20，离开内层后自动回到 10，离开外层回到默认。`set` 则用于「命令顶层一次性设置、进程退出即结束」的场景。

OpenLLM 在 `common.py` 顶层实例化了两个上下文变量：

- `VERBOSE_LEVEL = ContextVar(0)`：日志详细度阈值，默认 0（最简）。
- `INTERACTIVE = ContextVar(False)`：是否处于交互引导模式，默认关闭。

#### 4.3.2 核心流程

```text
ContextVar(0)        栈: []            get() → 默认 0
   │
   ├─ set(10)        栈: [10]          get() → 10
   │     │
   │     └─ with patch(20):           进入：栈: [10, 20]   get() → 20
   │              ...                 （离开：弹栈）栈: [10]  get() → 10
   │
   └─ （进程结束，栈无所谓）
```

一个关键不变量：**「当前生效值」永远是栈顶**；`patch` 保证压入的值一定会被弹掉，所以 `patch` 是「临时改值」的安全姿势。而连续两次 `set(20)` 会让栈长成 `[20, 20]`——读到仍是 20，但栈变厚了，所以 `set` 不适合用作「临时改完要还原」的场景，那正是 `patch` 的用武之地。

#### 4.3.3 源码精读

`ContextVar` 是一个泛型类，内部维护 `_stack` 列表与 `_default`：

[src/openllm/common.py:33-52](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py#L33-L52) 看三个方法的实现：`get` 在 `self._stack` 非空时返回 `[-1]`（栈顶），否则返回 `_default`；`set` 执行 `self._stack.append(value)`——这就是「压栈而非覆盖」的来源；`patch` 用 `@contextmanager` 装饰，`yield` 前 `append`、`finally` 里 `pop`，保证异常路径也能还原。

两个实例在定义类之后紧接着声明：

[src/openllm/common.py:55-56](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py#L55-L56) `VERBOSE_LEVEL` 默认 0、`INTERACTIVE` 默认 `False`。

「被使用」的真实证据在 CLI 入口：全局回调把 `--verbose` 的值压入 `VERBOSE_LEVEL`：

[src/openllm/__main__.py:360-361](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py#L360-L361) 用户传 `--verbose 10` 时，`VERBOSE_LEVEL.set(10)` 把阈值压到栈顶，后续整条命令链的 `output` 都会读到 10。而像 `serve` 这类命令在带 `--verbose` 时会直接 `set(20)`：

[src/openllm/__main__.py:264-265](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py#L264-L265) 这里固定压入 20（最高档），让命令打印最详细的信息（含子进程原始错误）。`INTERACTIVE` 则在 `hello` 命令里被打开：

[src/openllm/__main__.py:224](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py#L224) 进入 `hello` 交互引导时 `INTERACTIVE.set(True)`，下游函数通过 `INTERACTIVE.get()` 判断当前是否要弹出交互选项。

> 为什么用「栈」而不是 Python 标准库的 `contextvars.ContextVar`？标准库那套主要服务于异步并发，跨任务隔离；OpenLLM 这里需要的是「同步、嵌套、可累积」的简单压栈语义，自己实现更直白可控。这是一个有意识的设计取舍。

#### 4.3.4 代码实践

1. **实践目标**：亲眼看清 `set` 是「压栈」、`patch` 是「压栈 + 自动弹栈」。
2. **操作步骤**：
   ```python
   import openllm.common as c
   print('默认:', c.VERBOSE_LEVEL.get())        # 0
   c.VERBOSE_LEVEL.set(10)
   print('set(10) 后:', c.VERBOSE_LEVEL.get())  # 10
   with c.VERBOSE_LEVEL.patch(20):
       print('patch(20) 内:', c.VERBOSE_LEVEL.get())  # 20
   print('patch 结束后:', c.VERBOSE_LEVEL.get())      # 回到 10（栈顶弹出）
   ```
3. **需要观察的现象**：`patch` 结束后值**自动回到 10**，而不是回到默认 0——因为外层的 `set(10)` 仍在栈里。
4. **预期结果**：输出依次为 `0 / 10 / 20 / 10`。
5. **进阶**：把上面的 `set(10)` 也换成 `with c.VERBOSE_LEVEL.patch(10):` 包住整段，观察离开后是否回到 0，体会 `patch` 相对 `set` 的「自动还原」优势。

#### 4.3.5 小练习与答案

- **练习 1**：如果在同一段代码里连续写三次 `VERBOSE_LEVEL.set(20)`，`get()` 返回什么？栈里有几个元素？
  - **答案**：`get()` 返回 20（栈顶仍是 20），但栈里有三个 20。这正说明 `set` 会累积，不适合「临时改完要还原」的场景。
- **练习 2**：`patch` 的 `finally: self._stack.pop()` 为什么必须放在 `finally` 而不是普通语句？
  - **答案**：为了保证「即使 `with` 块内抛异常」，压入的值也一定会被弹出，栈不会泄漏。这正是上下文管理器的核心价值。

---

### 4.4 output 输出与详细度控制

#### 4.4.1 概念说明

`output()` 是 OpenLLM 对终端打印的统一封装，做了两件特别的事：

1. **按详细度过滤**：每条消息带一个 `level`（「需要多详细才显示」），只有 `level ≤ VERBOSE_LEVEL.get()` 时才打印。默认 `VERBOSE_LEVEL=0`，所以默认只显示 `level=0` 的关键消息。
2. **结构化美化**：如果传入的不是字符串（而是 dict / Pydantic 模型等），就用 `pyaml` 把它美化成 YAML 再打印，方便人读。

底层真正往屏幕写字的是 `questionary.print`，它额外支持 `style` 参数给文字上色（如 `'red'`、`'green'`、`'orange'`）。

#### 4.4.2 核心流程

```text
output(content, level=0, style=None, end=None)
        │
        ├─ level > VERBOSE_LEVEL.get() ?  ── 是 ──► 直接 return（静默丢弃）
        │                否
        ▼
   content 是 str ?
        ├─ 是 ──► questionary.print(content, style, end='\n')
        └─ 否 ──► pyaml.pprint 写入 StringIO
                  └─ questionary.print(那段YAML, style, end='')
```

详细度分档（与后续 `RepoInfo.tolist` / `BentoInfo.tolist` 的分档一致）：

| `VERBOSE_LEVEL` | 含义 | 会显示的 `level` |
| --- | --- | --- |
| `0` | 默认，最简 | 仅 `0` |
| `10` | 一般详细 | `0`、`10` |
| `20` | 最详细（含子进程原始输出/错误） | `0`、`10`、`20` |

#### 4.4.3 源码精读

`output` 的过滤判定只有一行：

[src/openllm/common.py:59-71](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py#L59-L71) 注意 [第 62-63 行](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py#L62-L63)：`if level > VERBOSE_LEVEL.get(): return`——「消息等级严格大于阈值」就丢弃。默认 `level=0`，所以 `output('普通消息')` 总会显示（因为 `0 > 0` 为假）；而 `output('调试', level=10)` 在默认阈值下被丢弃。

非字符串内容的 YAML 美化分支：

[src/openllm/common.py:65-69](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py#L65-L69) 先用一个 `io.StringIO` 当缓冲，让 `pyaml.pprint` 把对象写成 YAML 字符串，再交给 `questionary.print` 输出。`sort_dicts=False, sort_keys=False` 保留字典原始顺序，不被字母序打乱。

「详细度被消费」的典型例子在数据模型的 `tolist()` 方法里——它们根据 `VERBOSE_LEVEL` 返回不同详细程度的字典，再由 `output` 美化打印。例如 `RepoInfo.tolist`：

[src/openllm/common.py:139-153](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py#L139-L153) `verbose<=0` 返回一行简短字符串；`<=10` 多带 `path`；`<=20` 再带 `server/owner/repo`；更高返回 `None`（不显示）。这正是 `openllm repo list` 加 `--verbose` 后信息越来越多的原因。`output` 与 `tolist` 通过 `VERBOSE_LEVEL` 这个共享上下文变量达成默契配合。

#### 4.4.4 代码实践

1. **实践目标**：感受 `level` 与 `VERBOSE_LEVEL` 的过滤关系，以及 dict 的 YAML 美化输出。
2. **操作步骤**：
   ```python
   import openllm.common as c
   c.output('关键消息 (level=0)')              # 默认阈值 0，会显示
   c.output('调试消息 (level=10)', level=10)   # 0 < 10，被过滤，不显示
   c.output('错误消息', style='red')           # 字符串 + 红色
   c.output({'name': 'llama', 'size': [1, 2, 3]})  # dict → YAML 美化
   # 抬高阈值再看
   with c.VERBOSE_LEVEL.patch(10):
       c.output('现在调试消息也显示了 (level=10)', level=10)
   ```
3. **需要观察的现象**：第 2 行在默认阈值下**不会**出现；dict 那行被打印成缩进的 YAML 而非 Python 的 `{'name': ...}`；进入 `patch(10)` 后调试消息出现，离开后又消失。
4. **预期结果**：依次看到「关键消息」「错误消息（红）」「YAML 形式的 dict」「patch 内的调试消息」。
5. 想看更高档：把 `patch(10)` 换成 `patch(20)`，再传 `level=20` 的消息验证。

#### 4.4.5 小练习与答案

- **练习 1**：`output('x', level=0)` 在默认阈值下会显示吗？为什么？
  - **答案**：会。因为过滤条件是 `level > VERBOSE_LEVEL.get()`，即 `0 > 0` 为假，不触发 `return`，所以默认 `level=0` 的消息总能显示。
- **练习 2**：把一个 Pydantic 模型实例（比如 `Config()`）直接传给 `output`，会发生什么？
  - **答案**：它不是 `str`，走 YAML 分支，`pyaml.pprint` 会把模型字段序列化成 YAML 打印出来。这就是为什么很多命令直接 `output(some_model.tolist())`——`tolist()` 返回字典，`output` 自动美化。

---

## 5. 综合实践

把本讲三件套串起来。请在一个**临时** `OPENLLM_HOME`（避免影响你真实配置）中编写并运行下面的脚本，对照预期解释每一行输出的有无：

```python
# 文件名：explore_common.py
import openllm.common as common

# (1) 配置读写：读出当前 repos
cfg = common.load_config()
common.output(cfg.tolist())                 # dict → YAML 美化打印
print('default_repo =', cfg.default_repo)

# (2) 默认 VERBOSE_LEVEL=0，这条 level=10 的消息会被过滤（不显示）
common.output('【这条 level=10 默认不显示】', level=10)

# (3) 临时把详细度抬到 20，观察 output 行为变化
with common.VERBOSE_LEVEL.patch(20):
    common.output('【patch 内 level=10，现在显示了】', level=10)
    common.output({'hint': 'dict 也会被 YAML 美化', 'repos': list(cfg.repos)}, level=10)

# (4) 离开 with 后栈被弹出，回到默认 0，level=10 再次被过滤
common.output('【离开 patch，level=10 又不显示了】', level=10)
print('当前 VERBOSE_LEVEL =', common.VERBOSE_LEVEL.get())  # 期望 0
```

**运行**：`OPENLLM_HOME=/tmp/openllm-exp python explore_common.py`

**需要观察与解释**：

1. 第 (1) 步应看到 `repos`（含 `default`/`nightly`）的 YAML 输出——验证 `load_config` + `output` 的 dict 美化。
2. 第 (2) 步那句**不应**出现——解释为 `10 > 0` 触发过滤。
3. 第 (3) 步两句**都应**出现，且 dict 是 YAML 形式——解释为 `patch(20)` 把栈顶抬到 20，`10 > 20` 为假故显示。
4. 第 (4) 步那句**不应**出现，且末尾打印 `VERBOSE_LEVEL = 0`——解释为 `patch` 的 `finally` 弹栈，回到 `set` 都没调用过的默认值 0。

如果某一步与预期不符，先 `cat $OPENLLM_HOME/config.json`（可能不存在，这正是默认 `Config` 的来源），再回头核对 `common.py` 的过滤与弹栈逻辑。

## 6. 本讲小结

- `OPENLLM_HOME`（默认 `~/.openllm`，可被同名环境变量覆盖）是所有运行时路径的根；`repos/temp/venv` 三个目录在 `import openllm.common` 时即创建，而 `config.json` 只在 `save_config` 时落盘。
- `Config` 是描述用户偏好（`repos` + `default_repo`）的 Pydantic 模型；`load_config` 对「文件不存在 / 合法 / 损坏」三种情形都安全处理，`save_config` 用 `indent=2` 全量覆盖写。
- `ContextVar` 是 OpenLLM 自制的**栈式**上下文变量：`get` 读栈顶、`set` 压栈（非覆盖）、`patch` 是「压栈 + 自动弹栈」的安全上下文管理器。`VERBOSE_LEVEL`（默认 0）与 `INTERACTIVE`（默认 False）是它的两个实例。
- `output` 用 `level > VERBOSE_LEVEL.get()` 做过滤，非字符串内容经 `pyaml` 美化成 YAML，分档（0/10/20）与各数据模型 `tolist()` 的分档保持一致。
- CLI 全局回调通过 `VERBOSE_LEVEL.set(verbose)` 把 `--verbose` 写入上下文；这一条共享变量是「指挥层（`__main__.py`）」与「输出层（`output`/`tolist`）」之间的通信纽带。

## 7. 下一步学习建议

本讲只动了 `common.py` 的「静态状态层」。下一讲 **u2-l2 公共基础设施（二）：子进程与命令执行** 会继续留在 `common.py`，拆解 `run_command`（同步）、`async_run_command`（异步上下文管理器）、`stream_command_output`（流式输出），以及 `EnvVars` 如何排序去空、`bentoml`/`python` 命令如何被重写为模块调用。

在那之前，建议你：

- 动手做完本讲第 5 节的综合实践，确保理解「压栈 / 弹栈 / 过滤」三件事。
- 通读一遍 `src/openllm/common.py` 的第 1 到 100 行，把路径常量、`ContextVar`、`output`、`Config` 四块的相对位置记住——它们是后续所有命令的地基。
- 带着问题进入下一讲：既然 `output` 已经能打印，为什么 `run_command` 里还要单独处理 `bentoml` 这个命令名？
