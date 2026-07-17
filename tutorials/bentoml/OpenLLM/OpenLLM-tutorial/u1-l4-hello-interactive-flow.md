# 交互式上手：hello 命令引导流程

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 `openllm hello` 这条命令的完整编排：先探测本机硬件、再列出所有可用模型、然后让用户依次选择「模型 → 版本 → 动作」。
- 看懂 `can_run` 返回的分数如何驱动表格里「locally runnable（本地可运行）」那一列的勾选标记，以及为什么本地跑不动时只有 `deploy` 可选。
- 掌握 OpenLLM 用 `questionary`（交互选择）+ `tabulate`（表格渲染）组合出引导式体验的惯用手法，并能把这套手法用在自己写的 CLI 里。
- 能够阅读并小改一份 `hello` 的本地副本，在选择动作前额外打印当前选中 Bento 的 tag 与所需 GPU 信息。

本讲只读 `hello` 这一条命令链路，不涉及真正的模型加载与服务启动（那是 u3-l1 的内容）。

## 2. 前置知识

在进入源码前，先建立三个直觉。

### 2.1 什么是「引导式 CLI」

直接让用户敲 `openllm serve llama3.2:1b` 要求用户已经知道模型名、版本、仓库。而 `hello` 是一个「问答式」入口：它先问你想要哪个模型，再问要哪个版本，最后问你想 run / serve / deploy，选完直接帮你执行。本质上它是把 `serve`/`run`/`deploy` 三条命令的参数收集过程做成了一个交互向导。

### 2.2 Bento、BentoInfo、DeploymentTarget 三个数据对象

`hello` 的每一步选择，本质上都是在下面三类对象之间流转（定义都在 `common.py`）：

| 对象 | 含义 | 关键字段 |
|---|---|---|
| `BentoInfo` | 一个可运行的模型版本（来自某个模型仓库里的 `bento.yaml`） | `tag`、`name`、`version`、`repo`、`bento_yaml` |
| `DeploymentTarget` | 一个「运行目标」（本机或云端某实例） | `accelerators`、`platform`、`name`、`price` |
| `Accelerator` | 一块加速器（GPU） | `model`、`memory_size`（单位 GB） |

`can_run(bento, target)` 就是把一个 `BentoInfo` 放到一个 `DeploymentTarget` 上评估，返回一个 `float` 分数：**分数 > 0 表示「跑得动」**。这个正负号是本讲最关键的一个判断。

### 2.3 questionary 与 tabulate 的分工

- `questionary`：负责「在终端里弹出可上下选择的问题」，常用的是 `questionary.select(...)`、`questionary.Choice(...)`、`questionary.Separator(...)`。
- `tabulate`：负责「把一组数据排成对齐的文本表格」。

OpenLLM 在 `hello` 里把这两者拼在一起——用 `tabulate` 生成表格，再把表格的每一行塞进 `questionary` 的选项里，于是用户看到的是一张整齐的表格，又能用方向键选其中一行。这个「表格即选项」的小技巧后面会重点讲。

> 名词速查：`CHECKED` 是 `common.py` 里的常量，值为字符串 `'Yes'`，用来在表格里表示「打勾」。

## 3. 本讲源码地图

| 文件 | 本讲关注什么 |
|---|---|
| [src/openllm/__main__.py](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py) | `hello` 命令本体，以及它调用的四个交互函数 `_select_bento_name` / `_select_bento_version` / `_select_target` / `_select_action` |
| [src/openllm/accelerator_spec.py](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/accelerator_spec.py) | `get_local_machine_spec`（探测本机硬件）与 `can_run`（打分），是「本地可运行」标记的来源 |
| [src/openllm/model.py](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/model.py) | `list_bento`，`hello` 用它把模型仓库里的所有 Bento 扫出来 |
| [src/openllm/common.py](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py) | `BentoInfo` / `DeploymentTarget` / `Accelerator` 数据模型、`CHECKED` 常量、`output` 输出函数、`INTERACTIVE` 上下文开关 |

---

## 4. 核心概念与源码讲解

### 4.1 hello 命令主流程

#### 4.1.1 概念说明

`hello` 是 OpenLLM 的「新手村入口」。它不接收要运行的模型名作为参数（对比 `serve`/`run`/`deploy` 都需要 `model` 参数），而是通过一连串交互问题，帮用户从「我有什么硬件」「有哪些模型」一步步收敛到「执行哪个动作」。

它的设计目标有两个：

1. **降低上手门槛**：用户不需要记住模型 tag，跟着提示选就行。
2. **教学作用**：每完成一次引导，最后都会打印出等价的非交互命令（如 `openllm serve xxx`），鼓励用户下一次直接用命令行——相当于「扶上马送一程」。

#### 4.1.2 核心流程

`hello` 的执行可以画成下面这条流水线：

```
cmd_update()                      # 1. 更新模型仓库（保证模型列表是最新的）
INTERACTIVE.set(True)             # 2. 打开「交互模式」开关（影响后续 deploy 的行为）
target = get_local_machine_spec() # 3. 探测本机硬件 → DeploymentTarget（带缓存）
打印 platform / accelerators      # 4. 把探测结果展示给用户
models = list_bento(repo_name=repo)  # 5. 扫出所有 Bento → list[BentoInfo]
bento_name, repo = _select_bento_name(models, target)        # 6. 选「模型名」
bento, score  = _select_bento_version(models, target, ...)   # 7. 选「版本」
_select_action(bento, score, ...)                            # 8. 选「动作」并执行
```

注意三个细节：

- 第 3 步 `get_local_machine_spec()` 被 `@functools.lru_cache` 装饰，所以一次 CLI 运行里只会真正探测一次硬件。
- 第 5 步若扫不到任何模型，会提示用户执行 `openllm repo update` 并退出。
- 第 7 步返回的 `score` 会被第 8 步用来决定哪些动作可选（见 4.3）。

#### 4.1.3 源码精读

先看 `hello` 的函数签名与参数。它只接收 `repo`（指定从哪个仓库扫描）和三个透传给部署阶段的选项（`--env`/`--arg`/`--context`），不接受模型名：

[src/openllm/__main__.py:206-222](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py#L206-L222) —— `hello` 的定义与参数列表。

接下来是主体。`cmd_update()` 先确保仓库是最新的；`INTERACTIVE.set(True)` 把交互开关压栈为 `True`（这个开关后面 `deploy` 会读）：

[src/openllm/__main__.py:223-224](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py#L223-L224) —— 更新仓库并打开交互模式。

然后探测硬件并把结果打印出来。注意这里对 `target.accelerators` 是否为空做了分支：有 GPU 就逐块打印型号和显存，没有就打印 `None`：

[src/openllm/__main__.py:226-233](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py#L226-L233) —— 探测本机并展示平台与加速器信息。

硬件探测本身在 `accelerator_spec.py` 里。`get_local_machine_spec` 会先用 `psutil` 判平台：macOS 直接返回空加速器列表（因为 macOS 通常没有 NVIDIA GPU）；Linux/Windows 才尝试用 `pynvml`（NVML 的 Python 绑定）枚举 GPU；任何异常都会被兜底成「空加速器 + 黄色告警」，而不是让命令崩溃：

[src/openllm/accelerator_spec.py:64-113](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/accelerator_spec.py#L64-L113) —— `get_local_machine_spec`：平台判定 + NVML 探测 + 异常兜底。

> 这就是为什么「无 GPU 也能跑 `openllm hello`」：探测失败只会得到一个空的 `target`，后续选择照常进行，只是所有需要 GPU 的模型都不会被打勾。

`hello` 的后半段是扫描模型 + 串联四个选择函数：

[src/openllm/__main__.py:235-243](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py#L235-L243) —— 列出模型并依次选择模型名、版本、动作。

其中 `list_bento(repo_name=repo)` 负责把指定仓库（默认是 `default` 仓库）`bentoml/bentos/*/*` 目录下所有带 `bento.yaml` 的子目录扫成 `BentoInfo` 列表：

[src/openllm/model.py:122-180](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/model.py#L122-L180) —— `list_bento`：glob 扫描 + 别名文件处理 + 去重。

#### 4.1.4 代码实践

**实践目标**：亲眼看到 `hello` 的「探测硬件」和「列出模型」两个阶段，确认无 GPU 环境也能走到选择步骤。

**操作步骤**：

1. 在已安装 `openllm` 的环境里运行：
   ```bash
   openllm hello
   ```
2. 观察命令输出的前几行（在弹出模型选择菜单之前）。

**需要观察的现象**：

- 一行绿色的 `Detected Platform: linux`（或 `macos`/`windows`）。
- 紧接着 `Detected Accelerators:` 后面要么列出每块 GPU 的型号与显存，要么是 `None`。
- 然后弹出一个可上下选择的模型列表（来自 `questionary`）。

**预期结果**：即便本机没有 NVIDIA GPU，命令也不会报错，而是显示 `Detected Accelerators: None`，并继续弹出模型选择菜单——只是此时菜单里大概率没有任何模型被打勾（因为大多数模型需要 GPU）。

> 如果连模型列表都为空（出现 `No model found` 红字），先执行一次 `openllm repo update` 再重试。具体行为受本地仓库缓存状态影响，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `get_local_machine_spec()` 要用 `@functools.lru_cache` 装饰？如果不加会怎样？

> **答**：一次 CLI 调用里可能多处需要本机硬件信息（`hello` 里要、`serve`/`run`/`deploy` 里也要）。每次都重新调 NVML 枚举 GPU 既慢又可能因重复 `nvmlInit/nvmlShutdown` 引入副作用。`lru_cache` 让它在进程内只探测一次，后续直接返回缓存结果。

**练习 2**：在 macOS 上运行 `hello`，`target.accelerators` 会是什么？为什么？

> **答**：会是空列表 `[]`。因为 `get_local_machine_spec` 在 `psutil.MACOS` 为真时直接 `return DeploymentTarget(accelerators=[], source='local', platform='macos')`，根本不会去调 NVML。

---

### 4.2 模型与版本交互选择

#### 4.2.1 概念说明

`hello` 把「选模型」拆成了两步：

1. **选模型名**（`_select_bento_name`）：同一个名字（如 `llama3.2`）可能在不同仓库、不同版本里都存在，这一步先把「名字 + 仓库」确定下来。
2. **选版本**（`_select_bento_version`）：在确定了名字和仓库后，再让用户从该名字下的多个版本（如 `1b`、`8b`）里挑一个具体的 `BentoInfo`。

这两步都用了同一套「tabulate 生成表格 → questionary 渲染成选项」的手法，并且都依赖 `can_run` 给每行算一个分数，用分数的正负决定是否打勾。

#### 4.2.2 核心流程

**选模型名**的流程：

```
对每个 model 算 score = can_run(model, target)
按 (repo, name) 分组，把同组的 score 相加   # 名字层面：只要有一个版本能跑，就打勾
生成 table_data：每行 = (name, repo, 'Yes' if 组分数>0 else '')
tabulate 渲染表头 + 数据行
questionary.select 弹出选择，返回 (name, repo)
```

注意「按 (repo, name) 分组求和」这一步：它保证了模型名层面的勾选是**乐观的**——只要这个模型名下有任意一个版本在本机能跑，这一行就显示 `Yes`。

**选版本**的流程类似，但不再分组，而是针对上一步选定的 `(bento_name, repo)` 过滤出所有版本，每个版本单独算分，最终返回 `(BentoInfo, score)`。

**`can_run` 打分逻辑**（这是「能不能跑」的核心）：

```
resource_spec = bento.yaml 里 services[0].config.resources
platforms     = labels.platforms（默认 'linux'）

if target.platform 不在 platforms:        return 0.0      # 平台不符，直接 0 分
if 没有声明资源（CPU 模型等）:              return 正数      # 视为可跑
if 需要 N 块 gpu_type:
    required_gpu = ACCELERATOR_SPECS[gpu_type]
    可用加速器 = target 里显存 >= required_gpu.memory_size 的那些
    if N > len(可用加速器):                return 0.0      # 数量不够，0 分
    score = required_gpu.memory_size * N / sum(所有加速器显存)
```

关键结论：**分数 > 0 ⟺ 本地可运行**。需要 GPU 但显存/数量不足会精确地得到 `0.0`。

用公式表达 GPU 情形下的分数（记 \(m\) 为所需单卡显存，\(N\) 为所需卡数，\(M=\sum ac.\text{memory\_size}\) 为目标机上所有加速器显存总和）：

\[
\text{score} = \frac{m \cdot N}{M}
\]

#### 4.2.3 源码精读

先看选模型名。第 33 行算出每个模型的分数；第 34-36 行用 `defaultdict` 按 `(repo, name)` 累加分数；第 37-39 行生成表格行，分数大于 0 的填 `CHECKED`（即 `'Yes'`）：

[src/openllm/__main__.py:33-39](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py#L33-L39) —— 算分、按名分组累加、生成带勾选标记的表格行。

然后是「表格即选项」的核心手法。`tabulate(...)` 返回一个多行字符串，`.split('\n')` 把它切成行数组：`table[0]` 是表头、`table[1]` 是 `---` 分隔线，二者被塞进一个 `questionary.Separator`（不可选的分隔行）；`table[2:]` 是真正的数据行，每行对应一个 `questionary.Choice`。`zip(table_data, table[2:])` 把「原始数据」和「渲染后的文本行」对齐起来：

[src/openllm/__main__.py:43-53](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py#L43-L53) —— tabulate 渲染表头作为 Separator，数据行作为 Choice，开启搜索过滤。

> 这里有个容易看漏的点：每个 `Choice` 的 `value=value[:2]`。`value` 是 `table_data` 里的元组 `(name, repo, CHECKED)`，`[:2]` 取前两个元素即 `(name, repo)`。所以用户选中某行后，`questionary.select(...).ask()` 返回的是 `(name, repo)` 这个二元组——刚好是下一步 `_select_bento_version` 需要的入参。

选版本的逻辑结构几乎一样，区别在于：它先用 `model.name == bento_name and model.repo.name == repo` 过滤，且每个 `Choice` 的 value 是 `(BentoInfo, score)`（来自 `model_infos`），所以最终返回 `(BentoInfo, score)`：

[src/openllm/__main__.py:64-74](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py#L64-L74) —— 按选定名字/仓库过滤版本并算分。

[src/openllm/__main__.py:80-88](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py#L80-L88) —— 同样的「表格即选项」手法，返回 `(BentoInfo, score)`。

`can_run` 的打分实现集中在 `accelerator_spec.py`。先读资源和平台标签；平台不符直接返回 `0.0`；接着是 GPU 分支——用 `ACCELERATOR_SPECS[gpu_type]` 查到所需单卡显存，过滤出目标机上「显存足够」的卡，数量不够返回 `0.0`，否则按上面的分数公式返回：

[src/openllm/accelerator_spec.py:124-149](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/accelerator_spec.py#L124-L149) —— `can_run`：平台校验、资源解析、GPU 显存/数量校验与打分。

其中 `ACCELERATOR_SPECS` 是一张「gpu_type 字符串 → Accelerator」的硬编码规格表，例如 `'nvidia-tesla-t4'` 对应 16GB、`'nvidia-a100-80g'` 对应 80GB：

[src/openllm/accelerator_spec.py:35-61](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/accelerator_spec.py#L35-L61) —— 加速器规格字典，把仓库里的 gpu_type 映射到具体显存。

而 `CHECKED` 常量定义在 `common.py`，就是字符串 `'Yes'`，所以表格里「locally runnable」列显示的不是符号而是文字 `Yes`：

[src/openllm/common.py:28](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py#L28) —— `CHECKED = 'Yes'`。

#### 4.2.4 代码实践

**实践目标**：在不真正部署的前提下，亲手调用 `can_run`，理解分数的含义。

**操作步骤**（示例代码，非项目原有代码）：

```python
# 示例代码：手动构造 target 与 bento，观察 can_run 分数
from openllm.accelerator_spec import get_local_machine_spec, can_run, ACCELERATOR_SPECS
from openllm.common import DeploymentTarget, Accelerator
from openllm.model import list_bento

target = get_local_machine_spec()
print('本机:', target.platform, target.accelerators_repr)

for bento in list_bento()[:5]:
    print(bento.tag, '->', can_run(bento, target))
```

**需要观察的现象**：

- 本机若无 GPU，所有「需要 GPU」的模型分数应为 `0.0`。
- 若手动构造一个「显存很大的假 target」，原本 `0.0` 的模型分数会变成正数。

**预期结果**：分数的正负与表格里是否打勾完全一致；分数的绝对大小反映「资源宽裕程度」（分母是本机总显存，机器越强、同样模型分数越接近一个上界）。本实践依赖本地是否安装了 `pynvml` 与能否拉到模型仓库，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `_select_bento_name` 要按 `(repo, name)` 把分数「相加」，而不是取最大值或判断是否全为正？

> **答**：因为模型名层面的问题是「这个模型在我机器上有没有任何一个版本能跑」。把同名的多个版本分数相加，只要有一个版本分数为正，和就为正，这一行就会被打勾——这是符合直觉的「乐观」策略。取最大值也能达到同样正负效果，但相加实现起来更直观（一行 `+= score`）。

**练习 2**：一个 `gpu_type='nvidia-tesla-t4'`、`gpu=2` 的 Bento，在本机只有 1 块 16GB T4 时，`can_run` 会返回什么？

> **答**：返回 `0.0`。因为 `required_gpu.memory_size=16`，过滤后「显存 ≥ 16 的卡」只有 1 块，而 `resource_spec.gpu=2 > 1`，命中 `return 0.0` 分支，判定为跑不动。

**练习 3**：`questionary.Choice(line, value=value[:2])` 里的 `value[:2]` 起什么作用？

> **答**：`value` 是 `table_data` 的三元组 `(name, repo, CHECKED)`，`[:2]` 丢掉勾选标记，只把 `(name, repo)` 作为该选项被选中后返回的值。这样用户选完一行，函数直接拿到下一步需要的「名字 + 仓库」。

---

### 4.3 动作选择(run/serve/deploy)

#### 4.3.1 概念说明

选好具体的 `BentoInfo` 后，最后一步是「拿它做什么」。`_select_action` 提供三个动作：

| 动作 | 含义 | 实际调用 |
|---|---|---|
| `run` | 在终端里和模型对话 | `local_run(bento, port=随机, ...)` |
| `serve` | 本地起一个 OpenAI 兼容的聊天服务（带浏览器 Chat UI） | `local_serve(bento, ...)` |
| `deploy` | 部署到 BentoCloud | `cloud_deploy(bento, target, ...)` |

这里有个贴心的设计：**前一步算出的 `score` 决定了哪些动作可选**。`score > 0`（本机能跑）时，三个动作都可选；`score <= 0`（本机跑不动）时，`run` 和 `serve` 会被标记为 `disabled='insufficient res.'`，只能选 `deploy`（交给云端去跑）。

另一个贯穿设计是「毕业提示」：无论选哪个动作，执行完后都会在 `finally` 块里打印一条等价的非交互命令，鼓励用户下次直接用命令行。

#### 4.3.2 核心流程

```
if score > 0:
    构造三个「全部可选」的 Choice（run / serve / deploy）
else:
    构造 Choice，其中 run / serve 标记 disabled='insufficient res.'，仅 deploy 可选
action = questionary.select('Select an action', options).ask()

根据 action 分发：
  run    -> 随机端口(30000~40000) + local_run(...)
  serve  -> local_serve(...)
  deploy -> get_cloud_machine_spec() -> _select_target() -> cloud_deploy(...)
无论哪个动作：finally 打印 "Use this command to run the action again" + 等价命令
```

`deploy` 路径会多一步 `_select_target`：它把云端实例类型按 `can_run` 降序排序，再用同样的「表格即选项」手法让用户选一个实例，表格列包括实例名、加速器、每小时价格、是否可部署。

#### 4.3.3 源码精读

先看动作菜单的构造。`score > 0` 时三个选项都启用，每个 `Choice` 带 `shortcut_key`（按数字键 0/1/2 直接选），并用 `Separator` 在选项之间插入等价命令预览（如 `$ openllm run {bento}`）：

[src/openllm/__main__.py:137-154](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py#L137-L154) —— 本机可跑时，三个动作全部可选。

`score <= 0` 时，`run` 与 `serve` 的 `Choice` 多了 `disabled='insufficient res.'`，只有 `deploy` 可选——这就实现了「本地跑不动就只能上云」的引导：

[src/openllm/__main__.py:155-177](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py#L155-L177) —— 本机跑不动时，run/serve 被禁用，仅 deploy 可选。

选定动作后分发执行。注意 `run` 用 `random.randint(30000, 40000)` 随机选一个端口避免冲突；`deploy` 会先拉云端实例类型再让用户选 target。每个分支都用 `try/finally`，确保即使执行出错或被中断，也会打印「下次可以直接用的命令」：

[src/openllm/__main__.py:181-203](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py#L181-L203) —— 按 action 分发到 local_run / local_serve / cloud_deploy，并在 finally 打印等价命令。

以 `run` 分支为例，`finally` 里的「毕业提示」长这样：

[src/openllm/__main__.py:186-187](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py#L186-L187) —— 提示用户下次可直接用 `openllm run {bento}`。

`deploy` 路径里的 `_select_target` 同样用 tabulate + questionary，但列换成「实例类型 / 加速器 / 每小时价格 / 是否可部署」，并先用 `targets.sort(key=lambda x: can_run(bento, x), reverse=True)` 把最合适的实例排到最前：

[src/openllm/__main__.py:97-113](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py#L97-L113) —— 云端实例按可运行性排序并渲染成表格。

#### 4.3.4 代码实践

**实践目标**：理解「分数驱动动作可选性」这条规则，并验证它。

**操作步骤**（源码阅读型实践，无需真跑模型）：

1. 打开 [src/openllm/__main__.py:137-177](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py#L137-L177)，对比 `score > 0` 与 `else` 两个分支下三个 `Choice` 的差别。
2. 在本地副本里，临时把 `_select_action` 入口的 `score` 强制改成 `0.0`（例如在函数第一行加一句 `score = 0.0`），再运行 `openllm hello` 走到动作选择那一步。

**需要观察的现象**：

- 改之前（若本机能跑某小模型）：run/serve/deploy 都可选。
- 改之后：run 和 serve 显示为灰色且标注 `insufficient res.`，只能选 deploy。

**预期结果**：动作菜单的可选项完全由 `score` 正负决定，验证了「本地可运行性 → 动作可用性」的串联。注意改完后记得还原本地副本，不要提交。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `run` 和 `serve` 在本机跑不动时要被禁用，而 `deploy` 不禁用？

> **答**：`run`/`serve` 都是在**本机**执行推理（分别走 `local_run`/`local_serve`），本机资源不足就真的跑不起来，所以禁用以免用户白等。`deploy` 是把模型部署到 **BentoCloud**，用的是云端实例的资源，不受本机限制，所以始终可选——这正是 `hello` 在「本机没 GPU」场景下仍然有价值的兜底路径。

**练习 2**：三个 `Choice` 都带 `shortcut_key='0'/'1'/'2'`，这给用户带来什么便利？

> **答**：用户在动作菜单里可以直接按数字键 0/1/2 选中对应动作，而不必用方向键移动后回车，交互更快捷。

**练习 3**：为什么每个动作分支都要包在 `try/finally` 里，而不是执行完直接打印提示？

> **答**：因为 `local_run`/`local_serve`/`cloud_deploy` 可能抛异常或被用户 `Ctrl+C` 中断。用 `finally` 能保证「无论成功、失败还是中断，都打印出等价的非交互命令」，让用户下次可以脱离 `hello` 直接用命令行重试——这正是 `hello` 的「教学/毕业」设计意图。

---

## 5. 综合实践

把本讲的知识串起来，做一个小改造：**在选择动作之前，额外打印当前选中 Bento 的 tag 与所需 GPU 信息**。

**实践目标**：验证你已经理解 `hello` 的编排顺序，并熟悉 `BentoInfo` 的几个关键属性。

**操作步骤**：

1. 复制一份源码到本地副本（不要改动原仓库文件，便于还原）：
   ```bash
   cp src/openllm/__main__.py /tmp/__main__.copy.py
   ```
2. 在 `hello` 函数里定位到这一行（[src/openllm/__main__.py:242-243](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py#L242-L243)）：
   ```python
   bento, score = _select_bento_version(models, target, bento_name, repo)
   _select_action(bento, score, context=context, envs=envs, arg=arg, interactive=INTERACTIVE.get())
   ```
3. 在两行之间插入两行打印（示例代码）：
   ```python
   bento, score = _select_bento_version(models, target, bento_name, repo)
   output(f'[debug] selected bento tag = {bento.tag}, repo = {bento.repo.name}', style='green')
   output(f'[debug] required GPU = {bento.pretty_gpu or "CPU / no GPU requirement"}', style='green')
   _select_action(bento, score, context=context, envs=envs, arg=arg, interactive=INTERACTIVE.get())
   ```
   这里用到 `BentoInfo.tag`（[src/openllm/common.py:171-175](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py#L171-L175)）与 `BentoInfo.pretty_gpu`（[src/openllm/common.py:227-241](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py#L227-L241)，它读 `bento_yaml['services'][0]['config']['resources']` 并格式化成如 `16G` 或 `80Gx2`）。
4. 用修改后的副本运行（示例命令，具体路径按你的环境调整）：
   ```bash
   python -m openllm hello
   ```

**需要观察的现象**：

- 在你选完版本、动作菜单弹出**之前**，终端先打印两行 `[debug] ...`，分别显示选中的 `tag`（如 `llama3.2:1b`）和所需 GPU（如 `8G`，或 CPU 模型时显示 `CPU / no GPU requirement`）。

**预期结果**：

- `bento.tag` 形如 `名字:版本`，与表格里「version」列一致。
- `bento.pretty_gpu` 与 `openllm model list` 输出里「required GPU RAM」列一致（如 `8G`、`80Gx2`）。
- 这两行确实出现在动作选择菜单之前，印证了 `hello` 的执行顺序是「选模型名 → 选版本 → （你的打印）→ 选动作」。

> 这个实践不需要真的加载模型，只要能走到动作选择菜单即可，因此无 GPU 环境也能完成；真实可运行性**待本地验证**。

---

## 6. 本讲小结

- `hello` 是 OpenLLM 的交互式向导入口，编排顺序固定为：更新仓库 → 打开交互开关 → 探测本机硬件 → 扫描所有 Bento → 选模型名 → 选版本 → 选动作并执行。
- 硬件探测 `get_local_machine_spec` 带 `lru_cache`、对 macOS 和无 GPU 机器都有兜底，所以无 GPU 也能跑到选择步骤。
- 「本地可运行」标记完全由 `can_run(bento, target)` 的分数正负决定：需要 GPU 但显存/数量不足会精确返回 `0.0`，否则返回正分数。
- `_select_bento_name` 按 `(repo, name)` 累加分数，保证模型名层面的勾选是「乐观」的（有一个版本能跑就打勾）。
- 四个 `_select_*` 函数都用了同一个「tabulate 渲染表头作为 Separator、数据行作为 Choice」的手法，把对齐表格塞进 `questionary` 选择菜单。
- `_select_action` 用 `score` 的正负控制 run/serve/deploy 的可选性，并在 `finally` 里打印等价的非交互命令，引导用户「毕业」到直接用 `serve`/`run`/`deploy`。

## 7. 下一步学习建议

本讲把 `hello` 这条交互链路读完了，但链路里调用到的几个底层函数还只是「黑盒」。建议接着往下学：

- **`can_run` 与硬件规格的完整细节**：进阶层 [u2-l5 加速器规格与可运行性判定](u2-l5-accelerator-spec.md) 会把 `ACCELERATOR_SPECS`、`Resource`、`get_local_machine_spec`、`can_run` 的每个分支讲透。
- **`list_bento` 与 `BentoInfo` 如何从 `bento.yaml` 派生**：进阶层 [u2-l4 模型发现与 Bento 解析](u2-l4-model-discovery.md) 讲清楚模型扫描、别名机制与 `ensure_bento`。
- **真正的本地运行链路**：当你选了 `run`/`serve` 之后发生了什么？专家层 [u3-l1 本地 serve 与 run 的完整链路](u3-l1-local-serve-run.md) 会拆解 `local_serve`/`local_run` 如何拼装 `bentoml serve` 命令、准备虚拟环境并在终端对话。
- 想立刻动手的话，可以先用 `openllm serve <一个小模型>` 替代 `hello` 里的 serve 动作，体验「非交互」直接执行的区别。
