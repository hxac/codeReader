# 云端部署 cloud.py

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 `openllm deploy` 这条命令背后「拼装 `bentoml deploy` 命令 + 处理环境变量 + 拷贝云配置 + 交给子进程执行」的完整链路。
- 厘清三类环境变量来源——`bento.yaml` 声明、当前 shell 的 `os.environ`、CLI 显式 `--env`——在 `_get_deploy_cmd` 中被处理的顺序，以及最终拼进命令行的优先级。
- 理解 `ensure_cloud_context` 如何检测 BentoCloud 登录状态、在交互模式下引导用户登录。
- 看懂 `get_cloud_machine_spec` 如何把 BentoCloud 的实例类型列表转换成统一的 `DeploymentTarget`，以及为什么要把 `.yatai.yaml` 拷贝到 `bento.repo.path/bentoml` 下。

本讲是专家层第二讲，承接 u3-l1（本地 `serve`/`run`）。本地路径解决「在我这台机器上跑起来」，本讲解决「把模型部署到 BentoCloud 这类托管平台，得到一个可弹性扩展的 OpenAI 兼容服务」。

## 2. 前置知识

本讲默认你已经掌握以下内容（来自前置讲义），这里只做最简回顾：

- **`run_command` 与命令重写**（u2-l2）：OpenLLM 把 `bentoml xxx` 统一改写成 `python -m bentoml xxx` 执行；`EnvVars` 是排序去空、可哈希的环境变量容器；`copy_env=True` 时会把 `os.environ` 与传入的 `env` 合并（传入者优先）。
- **`BentoInfo` 与两套名字**（u2-l4）：`tag` 是用户面向、别名感知的名字；`bentoml_tag` 恒为真实版本号（`name:version`），是传给 `bentoml serve`/`bentoml deploy` 的名字；`bento_yaml` 是懒加载的 `bento.yaml` 内容。
- **`DeploymentTarget` / `Accelerator` / `ACCELERATOR_SPECS` / `can_run`**（u2-l5）：`DeploymentTarget` 是「运行目标」的统一抽象，本机和云端实例都是它；`can_run(bento, target)` 返回 float，`> 0` 即可运行。

补充一个本讲要用到、但前面讲得较轻的概念：

- **BentoCloud / yatai**：BentoCloud 是 BentoML 官方的模型托管云；`yatai` 是其背后的服务端组件。登录后，凭证和当前上下文（context，含 endpoint）保存在本地一个 `.yatai.yaml` 文件里，`bentoml` 命令靠它来认证。本讲会反复碰到这个文件。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/openllm/cloud.py](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/cloud.py) | 本讲主角。拼装 `bentoml deploy` 命令、处理三层环境变量、检测/引导云登录、拉取实例类型、拷贝云配置。 |
| [src/openllm/common.py](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py) | 提供 `BentoInfo`、`DeploymentTarget`、`Accelerator`、`EnvVars`、`run_command`、`INTERACTIVE` 等基础设施。 |
| [src/openllm/accelerator_spec.py](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/accelerator_spec.py) | 提供 `ACCELERATOR_SPECS`（GPU 别名→显存对照表），云端实例的 `gpu_type` 靠它翻译成 `Accelerator`。 |
| [src/openllm/__main__.py](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py) | 顶层 `deploy` 命令与 `_select_target` 交互选择，是 `cloud.deploy` 的调用方。 |

整条部署链路的调用关系：

```
__main__.deploy  ──► ensure_bento          (名字 → BentoInfo，不传 target)
               ──► get_cloud_machine_spec  (拉取云端实例类型，转 DeploymentTarget)
               ──► _select_target          (交互选 / 自动推荐最合身的实例)
               ──► cloud.deploy
                     ├── ensure_cloud_context  (确保已登录 BentoCloud)
                     ├── _get_deploy_cmd       (拼命令 + 处理环境变量 + 拷贝 yatai 配置)
                     └── run_command           (交子进程执行 bentoml deploy)
```

## 4. 核心概念与源码讲解

### 4.1 部署命令拼装与环境变量处理

#### 4.1.1 概念说明

`bentoml deploy <bento>` 是真正执行部署的命令，OpenLLM 并不自己实现部署逻辑，而是**拼出这条命令再交给子进程**。拼装的关键难点不在命令本身，而在**环境变量**：一个模型要跑起来可能需要若干环境变量（最典型的是下载 gated 模型权重用的 `HF_TOKEN`），而这些变量的值可能来自三个地方：

1. **`bento.yaml` 的 `envs` 声明**：模型作者在 Bento 里写明「这个模型需要哪些环境变量」，有时还会给一个默认 `value`。
2. **当前 shell 的 `os.environ`**：用户在终端里已经 `export` 过的变量。
3. **CLI `--env`**：用户在 `openllm deploy ... --env NAME=value` 里显式传入的。

`_get_deploy_cmd` 的工作就是：把这三类来源按一套优先级规则，统一翻译成 `bentoml deploy` 命令行上的若干 `--env NAME=value` 参数。

> ⚠️ 注意区分两条「环境变量通道」：
> - 命令行上的 `--env NAME=value` 是 **bentoml deploy 的参数**，用来给**云端部署的 Bento**注入运行时环境变量；
> - 函数返回的 `EnvVars`（只含 `BENTOML_HOME`）是**本地子进程**的环境变量，决定 `bentoml` 命令去哪里读配置和 Bento。
>
> 两者目的不同，不要混淆。

#### 4.1.2 核心流程

`_get_deploy_cmd` 的处理顺序（按代码执行先后）：

1. 起手拼出命令头 `['bentoml', 'deploy', bento.bentoml_tag]`，并准备返回值 `env = EnvVars({'BENTOML_HOME': ...})`。
2. **先解析 CLI `--env`**，得到 `explicit_envs` 字典（`NAME=value` 直接拆；只有 `NAME` 时去 `os.environ` 取，取不到就红色报错退出）。
3. **再处理 `bento.yaml` 声明的 `envs`**：对那些「没被 CLI 覆盖、没有 yaml 默认值、也不在 `os.environ` 里」的变量，交互模式下弹框让用户输入，非交互模式下若无默认值就报错退出；其余的暂不处理。
4. **补 `os.environ`**：凡是在 `bento.yaml` 里声明过、且当前 `os.environ` 里存在的变量，都追加一条 `--env NAME=<os值>`。
5. **最后追加 CLI `explicit_envs`**：把第 2 步收集到的显式变量逐条追加。
6. 处理 `--instance-type`、`--context`，拷贝 yatai 配置（见 4.3），返回 `(cmd, env)`。

把第 3、4、5 步合起来看，**追加进 `cmd` 的 `--env` 顺序是**：`bento.yaml 提示项 → os.environ 项 → CLI 项`，CLI 项排在最后。

由于同一变量名在命令行上重复出现时，按惯例是「后写覆盖先写」（last-wins），所以最终生效优先级为：

\[ \text{CLI } \texttt{--env} \;>\; \texttt{os.environ} \;>\; \texttt{bento.yaml} \]

> 说明：OpenLLM 的代码只负责控制 `--env` 在 `cmd` 里的**追加顺序**；真正对重复 `--env` 取最后一个的实现，是 `bentoml deploy` 自身的参数解析行为。OpenLLM 靠「把 CLI 放最后」来保证它优先级最高。

#### 4.1.3 源码精读

命令头与返回的子进程环境（注意 `bentoml_tag` 而非别名 `tag`，且 `BENTOML_HOME` 被重定向到仓库内的 `bentoml` 目录）：

[cloud.py:20-32](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/cloud.py#L20-L32) — 构造 `bentoml deploy <bentoml_tag>` 命令头，并把子进程的 `BENTOML_HOME` 指向 `bento.repo.path/bentoml`。

第一步：解析 CLI `--env`，支持 `NAME=value` 与裸 `NAME` 两种写法：

[cloud.py:34-50](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/cloud.py#L34-L50) — 裸 `NAME` 时从 `os.environ` 取值，取不到则红色报错并 `typer.Exit(1)`。

第二步：处理 `bento.yaml` 的 `envs`。先算出「真正需要提示用户输入」的变量名集合 `required_env_names`——条件是**未被 CLI 覆盖、没有 yaml 默认值、且不在 `os.environ` 中**：

[cloud.py:52-63](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/cloud.py#L52-L63) — 这里用 `bento.bento_yaml.get('envs', [])` 而非 `bento.envs`，是为了在 yaml 没有 `envs` 字段时安全降级为空列表。

随后遍历 `required_envs`，对需要交互/补默认值的变量追加 `--env`：

[cloud.py:70-97](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/cloud.py#L70-L97) — `default` 的取值优先 `os.environ[name]`，其次 yaml 的 `value`；非交互且 `default == ''` 时直接退出。

第三、四步：补 `os.environ` 中已存在的声明变量，最后追加 CLI 显式变量。**这两步决定了追加顺序**：

[cloud.py:99-106](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/cloud.py#L99-L106) — 先追加 `os.environ` 项，再追加 `explicit_envs`（CLI）项，CLI 排最后。

实例类型与 context 选项：

[cloud.py:108-112](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/cloud.py#L108-L112) — `--instance-type` 取自选定的 `DeploymentTarget.name`。

#### 4.1.4 代码实践

**实践目标**：亲手验证三类环境变量的最终优先级。

**操作步骤**（源码阅读型，无需真实部署）：

1. 读 [cloud.py:34-106](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/cloud.py#L34-L106)，在纸上跟踪这样一个场景：某变量 `HF_TOKEN` 同时出现在三处——`bento.yaml` 的 `envs` 里（无默认值）、当前 shell `export HF_TOKEN=os_token`、命令行 `--env HF_TOKEN=cli_token`。
2. 逐段判断 `HF_TOKEN` 是否被加入 `explicit_envs`、是否进入 `required_env_names`、是否在第 99-102 行被追加、是否在第 105-106 行被追加，记录它最终在 `cmd` 里出现的次数与取值。

**需要观察的现象**：

- `explicit_envs = {'HF_TOKEN': 'cli_token'}`（来自第 2 步）。
- `HF_TOKEN in explicit_envs` 为真，故在 70-97 行的循环里被 `continue` 跳过，不会从这里追加。
- 在 99-102 行：`HF_TOKEN` 属于 `all_required_env_names` 且在 `os.environ` 中 → 追加 `--env HF_TOKEN=os_token`。
- 在 105-106 行：`explicit_envs` 里有它 → 追加 `--env HF_TOKEN=cli_token`。

**预期结果**：`cmd` 中 `HF_TOKEN` 出现两次，顺序为 `... --env HF_TOKEN=os_token ... --env HF_TOKEN=cli_token`，CLI 值在最后，按 last-wins 最终生效的是 `cli_token`。即优先级 **CLI `--env` > `os.environ` > `bento.yaml`**。

> 待本地验证：若你本地装了 `bentoml`，可用 `bentoml deploy --help` 确认其对重复 `--env` 的去重策略是否确为「取最后一个」。

#### 4.1.5 小练习与答案

**练习 1**：如果用户只写了 `--env HF_TOKEN`（裸名），而当前 shell 里并没有 `HF_TOKEN`，会发生什么？

**参考答案**：在 [cloud.py:42-49](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/cloud.py#L42-L49)，`os.environ.get(name)` 返回 `None`，代码会以红色输出「Environment variable 'HF_TOKEN' specified via --env but not found ...」并 `raise typer.Exit(1)` 退出。

**练习 2**：为什么 `_get_deploy_cmd` 用 `bento.bento_yaml.get('envs', [])` 而不是直接用 `bento.envs` 属性？

**参考答案**：`BentoInfo.envs` 属性直接取 `self.bento_yaml['envs']`（见 [common.py:193-195](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py#L193-L195)），若 yaml 没有 `envs` 键会 `KeyError`；而 cloud.py 这里用 `.get('envs', [])` 可以在缺失时安全降级为空列表，更健壮。

### 4.2 BentoCloud 登录与上下文

#### 4.2.1 概念说明

部署到云端的前提是「已经登录 BentoCloud」。登录状态由 `bentoml` 自己管理，OpenLLM 不重新发明轮子，而是**调用 `bentoml cloud` 子命令**来查询和登录。本模块三件套：

- `resolve_cloud_config()`：定位本地 `.yatai.yaml`（云配置/凭证文件）的路径。
- `get_current_context()`：查询当前默认 context 的名字（用于实例类型查询时标注来源）。
- `ensure_cloud_context()`：确保已登录；未登录时，交互模式引导用户登录，非交互模式打印指引后退出。

`context`（上下文）是 BentoCloud 的概念：一个 context 对应一个云 endpoint（比如你公司的私有云地址）。多 context 时可用 `--context` 指定部署到哪个云。

#### 4.2.2 核心流程

`ensure_cloud_context` 的判定逻辑：

```
执行 bentoml cloud current-context
        │
   成功？├── 是 ──► 打印「already logged in: <endpoint>」，正常返回
        │
        └── 否 ──► 打印「not logged in」
                    │
              INTERACTIVE？├── 否 ──► 打印「请运行 bentoml cloud login」+ 账号获取指引，Exit(1)
                           │
                           └── 是 ──► questionary 选择「我有账号 / 去注册」
                                       → 输入 endpoint 与 token
                                       → 执行 bentoml cloud login --api-token ... --endpoint ...
                                       成功打印「Logged in successfully」，失败 Exit(1)
```

它依赖 `INTERACTIVE` 这个上下文变量（u2-l1 讲过的栈式 `ContextVar`），由 `deploy` 函数在入口处 `INTERACTIVE.set(interactive)` 设定。

#### 4.2.3 源码精读

定位 `.yatai.yaml`——若设了 `BENTOML_HOME` 环境变量就在其下找，否则用默认的 `~/bentoml/.yatai.yaml`：

[cloud.py:13-17](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/cloud.py#L13-L17) — `resolve_cloud_config` 用 `BENTOML_HOME` 或家目录定位云配置。

查询当前 context 名字（失败返回 `None`，不抛错）：

[cloud.py:125-131](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/cloud.py#L125-L131) — `get_current_context` 解析 `current-context` 的 JSON 输出取 `name`。

登录检测与交互式引导（注意非交互分支给的是「行动指引」而非直接报错退出）：

[cloud.py:134-176](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/cloud.py#L134-L176) — `ensure_cloud_context`：已登录则绿色提示；未登录时按 `INTERACTIVE` 分叉，交互分支用 `questionary` 收集 endpoint/token 再调 `bentoml cloud login`。

值得注意的两个细节：一是它直接用 `subprocess.check_output`（同步、捕获输出）而非 u2-l2 的 `run_command`，因为这些是「查询/登录」类短命令，不需要 venv 与流式输出；二是非交互分支贴心地提示了 [https://cloud.bentoml.com](https://cloud.bentoml.com) 注册地址与 BYOC（自建集群）联系方式。

#### 4.2.4 代码实践

**实践目标**：观察登录检测的两条分支。

**操作步骤**：

1. 在未登录 BentoCloud 的机器上，写一个最小脚本：
   ```python
   # 示例代码：仅供观察 ensure_cloud_context 的行为
   from openllm.cloud import ensure_cloud_context
   from openllm.common import INTERACTIVE
   INTERACTIVE.set(False)   # 模拟非交互
   ensure_cloud_context()
   ```
2. 运行它，观察输出。
3. 把 `INTERACTIVE.set(False)` 改成 `INTERACTIVE.set(True)` 再运行（不要真的输入 token，按 Ctrl-C 退出即可）。

**需要观察的现象**：非交互模式下应看到红色「bentoml not logged in」、橙色 `$ bentoml cloud login` 指引与黄色账号获取提示，随后进程退出；交互模式下会弹出 questionary 选择菜单。

**预期结果**：与源码 [cloud.py:144-152](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/cloud.py#L144-L152)（非交互）和 [cloud.py:154-169](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/cloud.py#L154-L169)（交互）一致。

> 待本地验证：若你已 `bentoml cloud login` 过，则第一条分支会打印绿色「already logged in: <endpoint>」。

#### 4.2.5 小练习与答案

**练习 1**：`ensure_cloud_context` 在交互模式下，用户选择了「get an account in two minutes」后会怎样？

**参考答案**：仅打印一行黄色提示「Please visit https://cloud.bentoml.com to get your token」（见 [cloud.py:160-161](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/cloud.py#L160-L161)），随后**继续**要求输入 endpoint 与 token——它并不会中止流程，只是给一个引导。

**练习 2**：为什么 `ensure_cloud_context` 用 `subprocess.check_output` 而不是 u2-l2 封装的 `run_command`？

**参考答案**：因为它需要**捕获命令的 stdout**（登录状态 JSON、context 名）来继续处理，而 `run_command` 默认是把输出直接打到终端、返回 `CompletedProcess`；这里更适合用原生的 `check_output`。此外这些查询/登录命令与 venv 无关，不需要 `run_command` 的解释器重写能力。

### 4.3 实例类型获取与配置拷贝

#### 4.3.1 概念说明

部署前要回答「部署到哪种机器上」。BentoCloud 提供多种实例类型（instance type），每种有不同的 GPU、价格。`get_cloud_machine_spec` 拉取这份清单，并翻译成 OpenLLM 内部的统一抽象 `DeploymentTarget`——这样云端实例和本机就能用同一套 `can_run` 打分逻辑（u2-l5）来比较。

本模块还有一步关键动作：**把本地 `.yatai.yaml` 拷贝到 `bento.repo.path/bentoml/` 下**。这是为了让 `bentoml deploy` 子进程能在它的 `BENTOML_HOME` 里找到云凭证。

#### 4.3.2 核心流程

`get_cloud_machine_spec` 流程：

```
ensure_cloud_context()                      # 先确保已登录
执行 bentoml deployment list-instance-types -o json [--context ...]
解析 JSON，对每个实例类型 it 构造 DeploymentTarget：
   source='cloud', name=it['name'], price=it['price'], platform='linux'
   accelerators = 若 it 有 gpu 且 gpu_type 在 ACCELERATOR_SPECS 中
                   → [ACCELERATOR_SPECS[gpu_type]] * it['gpu']
                  否则 → []
失败 → 红色提示，返回 []
```

`accelerators` 用「同一规格重复 N 次」表示「N 张该型号 GPU」，正好契合 `DeploymentTarget` 用列表长度表示 GPU 数量、`can_run` 用列表统计显存的设计。

配置拷贝流程（发生在 `_get_deploy_cmd` 末尾）：

```
base_config = resolve_cloud_config()        # ~/.bentoml/.yatai.yaml
若不存在 → raise Exception('Cannot find cloud config.')
若 bento.repo.path/bentoml/.yatai.yaml 已存在 → 先删除（避免旧凭证残留）
把 base_config 复制到 bento.repo.path/bentoml/.yatai.yaml
```

#### 4.3.3 源码精读

拉取并翻译实例类型：

[cloud.py:179-210](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/cloud.py#L179-L210) — 注意 `accelerators` 用列表推导 `[ACCELERATOR_SPECS[it['gpu_type']] for _ in range(int(it['gpu']))]` 把「型号+数量」展成 N 个 `Accelerator`；`gpu_type` 不在表里时退化为空列表（CPU 实例）。失败时返回空列表而非抛错，调用方据此提示「No available instance type」。

`ACCELERATOR_SPECS` 是 GPU 别名→显存的对照表，`get_cloud_machine_spec` 完全依赖它把云端返回的 `gpu_type`（如 `nvidia-tesla-t4`）翻译成带显存的 `Accelerator`：

[accelerator_spec.py:35-61](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/accelerator_spec.py#L35-L61) — 同一型号可能有多个别名（如 `nvidia-a100-80g` 与 `nvidia-a100-80gb` 都映射到 80GB 的 A100）。

配置拷贝（`_get_deploy_cmd` 的收尾）：

[cloud.py:114-120](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/cloud.py#L114-L120) — 先删后拷，保证目标位置永远是最新凭证。

> **为什么拷到 `bento.repo.path/bentoml` 下？**
> 因为 [cloud.py:32](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/cloud.py#L32) 把子进程的 `BENTOML_HOME` 设成了 `f'{bento.repo.path}/bentoml'`。`bentoml` 命令默认在 `BENTOML_HOME` 下查找配置（含 `.yatai.yaml` 云凭证）。`BENTOML_HOME` 之所以要重定向到这里，是因为**这个 Bento 的产物本身就在该仓库的 `bentoml/bentos/...` 目录下**（见 u2-l3/u2-l4 的 `RepoInfo.path → BentoInfo.path` 链路），`bentoml deploy` 必须从这个 `BENTOML_HOME` 才能找到要部署的 Bento。既然 `BENTOML_HOME` 被搬到了仓库内，云凭证 `.yatai.yaml` 也必须跟着搬过去，子进程才能既找到 Bento、又完成云端认证。这就是拷贝的根因。

公开入口 `deploy` 把以上步骤串起来：

[cloud.py:213-224](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/cloud.py#L213-L224) — 设 `INTERACTIVE` → `ensure_cloud_context` → `_get_deploy_cmd` → `run_command`。注意 `cwd=None`：因为 `BENTOML_HOME` 已定位到仓库内，不需要再 `cd`。

#### 4.3.4 代码实践

**实践目标**：理解 `.yatai.yaml` 拷贝的必要性。

**操作步骤**（源码阅读型）：

1. 读 [cloud.py:27](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/cloud.py#L27) 与 [cloud.py:32](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/cloud.py#L32)、[cloud.py:114-120](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/cloud.py#L114-L120)。
2. 画一张关系图：`bento.repo.path/bentoml` 同时是 ① `BENTOML_HOME` 的指向、② Bento 产物（`bentos/<name>/<version>/`）的根、③ 拷贝后 `.yatai.yaml` 的落脚点。

**需要观察的现象**：三者指向同一个目录，所以子进程在这一处既能找到 Bento、又能找到云凭证。

**预期结果**：能用一句话解释——「因为 `BENTOML_HOME` 被重定向到仓库内的 `bentoml` 目录，而 `bentoml deploy` 既要从这里找 Bento 产物、又要从这里读 `.yatai.yaml` 云凭证，所以必须把凭证拷过来」。

#### 4.3.5 小练习与答案

**练习 1**：`get_cloud_machine_spec` 在 `bentoml deployment list-instance-types` 失败时为什么不抛异常而是返回 `[]`？

**参考答案**：见 [cloud.py:205-210](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/cloud.py#L205-L210)，它捕获 `CalledProcessError`/`JSONDecodeError` 后红色提示并返回空列表。调用方（`__main__.deploy` 的 [line 336-338](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py#L336-L338)）据此判断 `runnable_targets` 为空，再以更友好的「No available instance type, check your bentocloud account」退出。这种「底层返回空、上层决定如何提示」的分层让错误信息更贴切。

**练习 2**：若云端某实例的 `gpu_type` 不在 `ACCELERATOR_SPECS` 表里，`get_cloud_machine_spec` 会怎样处理？

**参考答案**：见 [cloud.py:197-201](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/cloud.py#L197-L201)，条件 `it['gpu_type'] in ACCELERATOR_SPECS` 为假时走 `else []`，即该实例被当成**没有 GPU**（`accelerators` 为空）。后续 `can_run` 对它按 CPU/无资源规则打分，需要 GPU 的模型在其上得分通常为 0。

## 5. 综合实践

**任务**：把本讲三个模块串起来，模拟一次「带环境变量的云端部署」命令拼装，并验证你的理解。

**操作步骤**：

1. 阅读顶层 `deploy` 命令 [__main__.py:299-349](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py#L299-L349)，注意它对 `ensure_bento` **没有传 `target`**（对比 `serve`/`run` 都传了），这与 u2-l4「deploy 不触发 can_run 资源提醒」的结论一致。
2. 跟踪 `openllm deploy llama3.2:1b --instance-type gpu.t4 --env HF_TOKEN=abc` 这条命令（假设已登录、`llama3.2:1b` 在默认仓库）：
   - `ensure_bento` 解析出 `BentoInfo`（得到 `bentoml_tag`）。
   - 因为指定了 `instance_type`，走 [line 323-331](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py#L323-L331) 分支，构造 `DeploymentTarget(accelerators=[], name='gpu.t4')` 直接 `cloud_deploy`。
   - `cloud.deploy` → `ensure_cloud_context`（已登录则放行）→ `_get_deploy_cmd`。
3. 在 `_get_deploy_cmd` 里，假设 `llama3.2:1b` 的 `bento.yaml` 声明了 `envs: [{name: HF_TOKEN}]`，且你 shell 里 `export HF_TOKEN=shell_token`。请预测最终 `cmd` 里 `HF_TOKEN` 相关片段的样子。
4. 写下你的预测后，对照 [cloud.py:34-106](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/cloud.py#L34-L106) 逐行核对。

**预期结果**：`cmd` 形如：

```
bentoml deploy llama3.2:1b --env HF_TOKEN=shell_token --env HF_TOKEN=abc --instance-type gpu.t4
```

（`os.environ` 的 `shell_token` 先追加，CLI 的 `abc` 后追加，CLI 最终生效。）最终经 `run_command` 改写为 `python -m bentoml deploy ...`，子进程带着 `BENTOML_HOME=<repo>/bentoml`、并在该目录下读到拷贝过去的 `.yatai.yaml` 完成部署。

> 待本地验证：真实部署需要 BentoCloud 账号与可用的 gated 模型访问权；无账号时可止步于「预测 cmd 片段」这一步，重点是理解拼装顺序。

## 6. 本讲小结

- `cloud.deploy` 是个很薄的编排函数：设 `INTERACTIVE` → `ensure_cloud_context` → `_get_deploy_cmd` → `run_command`，真正干活的还是 `bentoml deploy` 子进程。
- 环境变量有三个来源，按「追加进 `cmd` 的顺序」是 `bento.yaml 提示项 → os.environ 项 → CLI 项`，CLI 排最后，故最终优先级为 **CLI `--env` > `os.environ` > `bento.yaml`**。
- 命令行 `--env NAME=value` 是给**云端部署的 Bento**注入运行时变量；返回的 `EnvVars`（`BENTOML_HOME`）是**本地子进程**环境——两条通道不能混为一谈。
- `ensure_cloud_context` 靠 `bentoml cloud current-context` 检测登录状态，未登录时交互分支用 questionary 引导 `bentoml cloud login`，非交互分支打印指引后退出。
- `get_cloud_machine_spec` 把 `bentoml deployment list-instance-types` 的输出翻译成 `DeploymentTarget` 列表，`gpu_type` 经 `ACCELERATOR_SPECS` 变成带显存的 `Accelerator`，列表长度即 GPU 数量。
- `.yatai.yaml` 之所以要拷到 `bento.repo.path/bentoml` 下，是因为 `BENTOML_HOME` 被重定向到了那里，子进程既要在这里找 Bento 产物、又要在这里读云凭证。

## 7. 下一步学习建议

- **横向对比本地路径**：回头读 `src/openllm/local.py` 的 `prep_env_vars` 与 `_get_serve_cmd`（[local.py:21-43](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/local.py#L21-L43)），对比本地 `serve` 与云端 `deploy` 在「BENTOML_HOME 重定向」「环境变量注入」上的异同。
- **继续专家层**：下一讲 u3-l3 将精读 `analytic.py`，看 `OpenLLMTyper` 如何给每条命令（包括本讲的 `deploy`）自动裹上「使用埋点 + 计时」。
- **延伸阅读**：若对 Bento 部署细节感兴趣，可阅读 `bentoml` 的 `deploy` 子命令文档，理解 `--env`、`--instance-type`、`--context` 等参数在云端的真实语义，与本讲 OpenLLM 的拼装逻辑相互印证。
