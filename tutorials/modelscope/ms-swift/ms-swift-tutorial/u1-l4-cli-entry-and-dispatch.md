# CLI 入口与命令分发

## 1. 本讲目标

本讲要回答一个看似简单但其实很关键的问题：**当你在终端敲下 `swift sft --model ... ` 时，这行命令到底是怎么跑起来的？**

学完本讲，你应当能够：

- 说清楚 `swift` 这个命令是从哪里「长出来」的（`setup.py` 的 `console_scripts` 注册）。
- 理解 `cli_main` 如何用一张 `ROUTE_MAPPING` 路由表，把子命令（`sft`、`infer`、`export`……）映射到具体的脚本文件。
- 掌握「多卡训练用 `torchrun`、单卡直接 `python`」的判定逻辑 `use_torchrun`，并能预测一条命令会被展开成什么样子。
- 理解 `parse_yaml_args` 如何把一个 YAML/JSON 配置文件就地展开成一串命令行参数。
- 解释为什么 `swift --help` 会报错，而 `swift sft --help` 却能正常打印帮助。

本讲是后续所有「训练 / 推理 / 导出」讲义的地基：只要你看懂了这条分发链路，后面任何一个 `swift xxx` 命令对你来说都不再是黑盒。

## 2. 前置知识

本讲假设你已经读过前 3 讲，了解了 ms-swift 的能力矩阵、安装方式与包结构。除此之外，再补充几个概念：

- **CLI 与子命令**：CLI（Command-Line Interface）就是命令行程序。很多工具采用「一个主命令 + 多个子命令」的形式，例如 `git clone`、`git commit`。ms-swift 也是这种风格：主命令是 `swift`，子命令有 `sft`、`infer`、`export` 等。
- **torchrun 与多卡训练**：单卡训练只需要一个 Python 进程；多卡训练需要多个进程协同（每个 GPU 一个进程）。PyTorch 提供了 `torch.distributed.run`（命令名 `torchrun`）来帮你拉起这些进程、分配编号（rank）、建立通信。ms-swift 并没有自己造轮子，而是直接复用 `torchrun`。
- **子进程重启动（subprocess re-launch）**：本讲最核心的设计技巧。`swift` 命令本身不做真正的训练，它只是一个「发射器」——算清楚要执行哪个脚本、要不要套一层 `torchrun`，然后用 `subprocess.run` 把真正的脚本重新启动一遍。这样做的好处是把「要不要分布式」这件事集中在一个地方决策。
- **YAML / JSON 配置**：命令行参数一多，写在 shell 里又长又难维护。把它们写进一个 YAML 或 JSON 文件，再用 `swift sft config.yaml` 一行调用，是工程上常见的做法。本讲会看到 ms-swift 如何把配置文件「翻译」回命令行参数。

## 3. 本讲源码地图

本讲只涉及 `swift/cli/` 目录下的薄薄一层代码，它们就是整个分发链路的全部：

| 文件 | 作用 |
| --- | --- |
| `setup.py` | 通过 `console_scripts` 把 `swift` 命令注册到系统，指向 `swift.cli.main:cli_main`。 |
| `swift/cli/main.py` | 分发核心：`ROUTE_MAPPING` 路由表、`use_torchrun` 判定、`parse_yaml_args` 配置展开、`cli_main` 主流程。 |
| `swift/cli/utils.py` | 多卡下的单设备模式工具 `try_use_single_device_mode`。 |
| `swift/cli/sft.py` | 子命令 `sft` 真正的执行体（被 main.py 作为子进程启动）。其他子命令（`pt.py`/`infer.py`/`export.py`……）结构几乎相同。 |
| `swift/cli/_megatron/main.py` | `megatron` 命令的入口，复用 `cli_main` 但传入自己的路由表与 `is_megatron=True`。 |

记住一句话：**`main.py` 负责调度，`sft.py` 这类文件负责干活**。

## 4. 核心概念与源码讲解

### 4.1 入口注册与子命令路由 ROUTE_MAPPING

#### 4.1.1 概念说明

当你 `pip install` 完 ms-swift 后，终端里就能用 `swift` 命令。这个命令不是凭空出现的——它是 Python 打包时通过 `entry_points` 里的 `console_scripts` 字段注册出来的。其本质是：在系统的可执行目录下生成一个极小的启动脚本，它做的事就是「调用你指定的那个 Python 函数」。

ms-swift 把这个函数定为 `swift.cli.main:cli_main`，意思是「`swift/cli/main.py` 文件里的 `cli_main` 函数」。

`cli_main` 要做的第一件事，就是搞清楚用户到底想干什么。用户敲 `swift sft ...`，第二个词 `sft` 就是「子命令」。ms-swift 维护了一张 **路由表 `ROUTE_MAPPING`**，把每个合法子命令映射到一个 Python 模块路径，例如 `'sft' -> 'swift.cli.sft'`。看到 `sft`，就知道要去执行 `swift/cli/sft.py` 这个脚本。

这个设计与上一讲（u1-l3）讲的「基类 + 注册表 + CLI 开关」三件套一脉相承：这里 `ROUTE_MAPPING` 就是注册表，子命令名就是 CLI 开关。

#### 4.1.2 核心流程

`cli_main` 在路由阶段的核心流程是：

1. 读取 `sys.argv`（命令行全部参数），去掉最前面的程序名。
2. 取出第一个参数作为「子命令名」，并把其中的下划线 `_` 转成连字符 `-`（所以 `web_ui` 和 `web-ui` 等价）。
3. 用子命令名去 `ROUTE_MAPPING` 查表，得到模块路径（如 `swift.cli.sft`）。
4. 用 `importlib.util.find_spec(...).origin` 把模块路径解析成磁盘上的真实文件路径。
5. 把文件路径交给后续步骤（4.2 节）作为真正要执行的脚本。

> 小提示：这张表里没有 `--help` 这个键，所以 `swift --help` 会在查表这一步直接抛 `KeyError`；而 `swift sft --help` 里 `--help` 是传给 `sft.py` 的参数，由 argparse 处理，所以能正常打印帮助。

#### 4.1.3 源码精读

入口注册在 `setup.py` 的 `console_scripts` 里，注册了两个命令：`swift` 和 `megatron`，分别指向各自的 `cli_main`：

[setup.py:162-164](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/setup.py#L162-L164) —— 这一行把 `swift` 命令绑定到 `swift.cli.main:cli_main`。

路由表本体，子命令名 → 模块路径：

[swift/cli/main.py:14-27](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/cli/main.py#L14-L27) —— `ROUTE_MAPPING` 列出了全部 12 个子命令。注意 `web-ui`、`merge-lora` 用的是连字符，这就是为什么 `cli_main` 里要做 `_` → `-` 的替换。

`cli_main` 的开头几行完成了路由解析：

```python
def cli_main(route_mapping=None, is_megatron=False):
    route_mapping = route_mapping or ROUTE_MAPPING
    argv = sys.argv[1:]
    method_name = argv[0].replace('_', '-')
    argv = argv[1:]
    file_path = importlib.util.find_spec(route_mapping[method_name]).origin
```

[swift/cli/main.py:86-91](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/cli/main.py#L86-L91) —— `argv[0]` 是子命令，查表得到模块路径，再用 `find_spec().origin` 拿到脚本的绝对路径。

被路由到的真正执行体 `sft.py` 非常薄，所有逻辑都包在 `if __name__ == '__main__':` 里：

```python
if __name__ == '__main__':
    from swift.cli.utils import try_use_single_device_mode
    try_use_single_device_mode()
    try_init_unsloth()
    from swift.ray_utils import try_init_ray
    try_init_ray()
    from swift.pipelines import sft_main
    sft_main()
```

[swift/cli/sft.py:13-20](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/cli/sft.py#L13-L20) —— 注意 import 全部写在 `if` 块里。这是故意的「延迟导入」，让发射器 `main.py` 保持轻量，重型依赖（peft / ray / pipeline）只在真正干活的子进程里才加载。

#### 4.1.4 代码实践

**实践目标**：亲眼看到路由表是怎么工作的，并验证 `swift --help` 会失败而 `swift sft --help` 会成功。

**操作步骤**：

1. 确认已按 u1-l2 用 `pip install -e .` 装好 ms-swift。
2. 运行 `swift sft --help`，观察输出。
3. 运行 `swift --help`，观察输出。
4. 运行 `swift web-ui --help` 和 `swift web_ui --help`，对比两者是否一致。

**需要观察的现象**：

- 第 2 步会先打印一行形如 `run sh:` 的命令（这是 4.1.3 里 `cli_main` 的产物，下文会详解），随后才是 sft 的 argparse 帮助文档。
- 第 3 步会抛出异常，提示类似 `KeyError: '--help'`。
- 第 4 步两者结果一致，证明 `_` 被转成了 `-`。

**预期结果**：`swift` 后必须紧跟一个 `ROUTE_MAPPING` 里存在的子命令；不存在的子命令或 `--help` 这种「伪子命令」都会在查表时失败。

> 若你的环境未安装成功或行为与此不符，标记为「待本地验证」并记录实际报错。

#### 4.1.5 小练习与答案

**练习 1**：用户敲了 `swift` 后直接回车（没有任何子命令），会发生什么？

**参考答案**：`sys.argv[1:]` 为空，`argv[0]` 取不到值，会抛 `IndexError: list index out of range`。这说明 `swift` 命令本身没有「默认行为」，必须显式给子命令。

**练习 2**：`swift merge_lora --help` 能不能正常工作？为什么？

**参考答案**：能。`cli_main` 会把 `merge_lora` 的下划线替换成连字符得到 `merge-lora`，而 `merge-lora` 正是 `ROUTE_MAPPING` 里的合法键。

---

### 4.2 多进程启动判定与 torchrun

#### 4.2.1 概念说明

多卡训练和单卡训练的启动方式完全不同：单卡只需要 `python script.py`，而多卡需要 `python -m torch.distributed.run --nproc_per_node 8 script.py`（即 `torchrun`）。ms-swift 不想逼用户去记两套命令，于是用一个统一规则：**用户通过环境变量告诉框架「我要几张卡」，框架自己决定要不要套 torchrun**。

负责这件事的是两个函数：

- `use_torchrun()`：判断当前是否需要多进程。
- `get_torchrun_args()`：把相关环境变量收集成 `torchrun` 需要的参数列表。

#### 4.2.2 核心流程

判定与拼装的流程如下：

1. `use_torchrun()` 检查环境变量 `NPROC_PER_NODE`（每机进程数）和 `NNODES`（机器数）。只要有一个被设置了，就认为需要多进程。
2. 若需要多进程，`get_torchrun_args()` 把 `NPROC_PER_NODE`、`MASTER_PORT`、`NNODES`、`NODE_RANK`、`MASTER_ADDR` 这几个变量转成 `--key value` 形式。
3. `cli_main` 综合判断最终命令形态（见下面的关键条件）。

最关键的判断在 `cli_main` 的这一行（伪代码）：

```
若 (不需要 torchrun) 或 (不是 megatron 且 子命令不在 {pt, sft, rlhf, infer} 中):
    直接用 python 跑脚本           # 单进程分支
否则:
    用 python -m torch.distributed.run 跑脚本   # 多进程分支
```

用一个真值表来理解第二段条件（假设设置了 `NPROC_PER_NODE`，即「需要 torchrun」）：

| 子命令 | 是否 megatron | 走哪个分支 |
| --- | --- | --- |
| `sft` / `pt` / `rlhf` / `infer` | 否 | 多进程（torchrun） |
| `export` / `eval` / `app` / `deploy` … | 否 | 单进程（即使设了 `NPROC_PER_NODE` 也忽略） |
| 任意 megatron 子命令（`sft`/`pt`/`rlhf`/`export`） | 是 | 多进程（torchrun） |

这背后的工程直觉是：**只有训练和推理类命令才真正受益于多进程**；导出、评测、Web 界面这些命令套 torchrun 没有意义，所以即便用户设了环境变量，框架也会忽略。而 megatron 一族命令本身就是为分布式设计的，所以一律走 torchrun。

#### 4.2.3 源码精读

判定函数，只看两个环境变量：

```python
def use_torchrun() -> bool:
    nproc_per_node = os.getenv('NPROC_PER_NODE')
    nnodes = os.getenv('NNODES')
    if nproc_per_node is None and nnodes is None:
        return False
    return True
```

[swift/cli/main.py:30-35](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/cli/main.py#L30-L35) —— 注意是「或」关系：任意一个被设置即返回 True。

收集 torchrun 参数：

```python
def get_torchrun_args():
    if not use_torchrun():
        return
    torchrun_args = []
    for env_key in ['NPROC_PER_NODE', 'MASTER_PORT', 'NNODES', 'NODE_RANK', 'MASTER_ADDR']:
        env_val = os.getenv(env_key)
        if env_val is None:
            continue
        torchrun_args += [f'--{env_key.lower()}', env_val]
    return torchrun_args
```

[swift/cli/main.py:74-83](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/cli/main.py#L74-L83) —— 把大写环境变量名转成 torchrun 认识的小写长选项，例如 `NPROC_PER_NODE=8` 变成 `--nproc_per_node 8`。

主流程里的分支决策：

```python
if torchrun_args is None or (not is_megatron and method_name not in {'pt', 'sft', 'rlhf', 'infer'}):
    args = [python_cmd, file_path, *argv]
else:
    args = [python_cmd, '-m', 'torch.distributed.run', *torchrun_args, file_path, *argv]
```

[swift/cli/main.py:95-98](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/cli/main.py#L95-L98) —— 这就是上一节真值表对应的代码。`python_cmd = sys.executable` 保证用的是当前 Python 解释器。

构造完命令后，先打印再执行：

```python
print(f"run sh: `{' '.join(args)}`", flush=True)
result = subprocess.run(args)
if result.returncode != 0:
    sys.exit(result.returncode)
```

[swift/cli/main.py:99-102](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/cli/main.py#L99-L102) —— 那行 `run sh:` 就是用户在终端看到的「实际执行命令」，是排查问题最重要的线索。返回码非 0 时让 `swift` 以同样码退出，保证 shell 脚本能正确捕获训练失败。

多卡下每个进程如何「认领」自己的 GPU，靠这个工具函数：

```python
def try_use_single_device_mode():
    if os.environ.get('SWIFT_SINGLE_DEVICE_MODE', '0') == '1':
        visible_devices = os.environ.get('CUDA_VISIBLE_DEVICES')
        local_rank = os.environ.get('LOCAL_RANK')
        ...
        os.environ['CUDA_VISIBLE_DEVICES'] = str(visible_device)
        os.environ['LOCAL_RANK'] = '0'
```

[swift/cli/utils.py:5-14](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/cli/utils.py#L5-L14) —— 这是可选的高级模式：开启后每个 torchrun 拉起的子进程只看见自己那张卡（通过 `LOCAL_RANK` 选 `CUDA_VISIBLE_DEVICES` 里对应的那一块），把多卡拆成若干「逻辑单卡」。

#### 4.2.4 代码实践

**实践目标**：用「不真正训练」的方式，看到 `cli_main` 把命令展开成了 `torch.distributed.run` 形式。

**操作步骤**：

1. 运行（不需要真的有 8 张卡，因为我们只看打印的那行命令）：

   ```shell
   NPROC_PER_NODE=2 swift sft --help
   ```

2. 观察第一行 `run sh:` 打印的内容。
3. 对比运行（不带环境变量）：

   ```shell
   swift sft --help
   ```

   再看 `run sh:` 的区别。

**需要观察的现象**：

- 第 1 步的 `run sh:` 里应当出现 `python -m torch.distributed.run --nproc_per_node 2 .../sft.py --help`。
- 第 3 步的 `run sh:` 则是 `python .../sft.py --help`，没有 `torch.distributed.run`。

**预期结果**：仅凭环境变量 `NPROC_PER_NODE` 的有无，框架就自动切换了启动方式，用户命令本身完全不变。这正是「发射器」设计的价值。

> 说明：`--help` 会在 argparse 阶段就退出，不会真的拉起多进程训练，因此本实践可在无 GPU 环境安全观察。若你的 `--help` 行为不同，标记「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：设了 `NPROC_PER_NODE=4`，但运行的是 `swift export ...`，会用 4 个进程吗？

**参考答案**：不会。`export` 不在 `{pt, sft, rlhf, infer}` 里，也不是 megatron，所以走单进程分支，`NPROC_PER_NODE` 被忽略。

**练习 2**：为什么 `cli_main` 用 `sys.executable` 而不是写死 `python`？

**参考答案**：`sys.executable` 是当前正在运行 `swift` 命令的那个 Python 解释器的绝对路径。用它来启动子进程，可以保证子进程和发射器在同一个虚拟环境 / conda 环境里，避免「装了 ms-swift 的环境」和「子进程用的环境」不一致的坑。

---

### 4.3 YAML / JSON 配置解析 parse_yaml_args

#### 4.3.1 概念说明

训练命令往往有二三十个参数，全写在 shell 里既难读又难维护。ms-swift 允许把这些参数写进一个 YAML 或 JSON 文件，然后 `swift sft config.yaml` 一行调用。

`parse_yaml_args` 做的事情非常朴素：**如果第一个参数是一个配置文件，就把它读进来，按规则「翻译」成一串命令行参数，替换掉原文件名**。翻译完之后，剩下的流程（路由、torchrun）完全感知不到配置文件的存在——它们看到的都是普通的 `--key value`。

此外，配置里还支持一个特殊的 `ENV` 块，用来注入环境变量，这正好可以和 4.2 节的 `NPROC_PER_NODE` 联动。

#### 4.3.2 核心流程

配置展开的流程：

1. 判断 `argv[0]` 是否以 `.json` / `.yaml` / `.yml` 结尾；不是就直接返回（当作普通命令行参数）。
2. 读取文件内容为字典 `config`。
3. 记录配置文件路径到环境变量 `SWIFT_CONFIG_FILE`（后续保存配置时用到）。
4. 弹出特殊的 `ENV` 字典：对每个键值，若该环境变量尚未设置则注入；若已存在且值不同则打印警告（已存在的优先）。
5. 遍历剩下的 `k: v`，转成命令行形式：
   - `v` 是列表 → `--k v1 v2 v3`（如 `--dataset a b c`）。
   - `v` 是字典 → `--k '<json 字符串>'`。
   - `v` 是其他标量 → `--k str(v)`。
6. 用展开后的参数列表替换掉 `argv[0]`（即配置文件名）。

#### 4.3.3 源码精读

文件类型判定与读取：

```python
if argv[0].endswith('.json'):
    config = json.load(f)
elif argv[0].endswith('.yaml') or argv[0].endswith('.yml'):
    config = yaml.safe_load(f)
if config is None:
    return
os.environ['SWIFT_CONFIG_FILE'] = argv[0]
```

[swift/cli/main.py:42-51](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/cli/main.py#L42-L51) —— 既不是 json 也不是 yaml 就直接返回；否则记录配置路径到 `SWIFT_CONFIG_FILE`。

ENV 块注入，已存在的环境变量优先：

```python
env = config.pop('ENV', None)
if env:
    for k, v in env.items():
        if k not in os.environ:
            os.environ[k] = str(v)
        elif str(v) != os.environ[k]:
            logger.warning(f'{k} is already set in environment, using `{os.environ[k]}` instead of `{v}`')
```

[swift/cli/main.py:53-59](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/cli/main.py#L53-L59) —— 这里实现了一个很有用的策略：shell 里显式导出的变量优先级高于配置文件，避免配置文件「偷偷覆盖」运维环境。

普通字段的展开规则：

```python
config_argv = []
for k, v in config.items():
    config_argv.append(f'--{k}')
    if isinstance(v, list):
        config_argv += v
    else:
        if isinstance(v, dict):
            v = json.dumps(v, ensure_ascii=False)
        else:
            v = str(v)
        config_argv.append(v)
argv[0:1] = config_argv
```

[swift/cli/main.py:60-71](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/cli/main.py#L60-L71) —— 列表直接展开（这正是多数据集写法的来源）；字典转成 JSON 字符串（用于 `--loss_scale` 之类结构化参数）；其余转字符串。最后 `argv[0:1] = config_argv` 把文件名原地替换为参数列表。

一个真实示例，配置文件里：

```yaml
dataset:
  - 'AI-ModelScope/alpaca-gpt4-data-zh#500'
  - 'swift/self-cognition#500'
deepspeed: zero2
```

[examples/yaml/deepspeed/sft.yaml:12-16](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/yaml/deepspeed/sft.yaml#L12-L36) —— 经过 `parse_yaml_args` 后，会被展开成 `--dataset AI-ModelScope/alpaca-gpt4-data-zh#500 swift/self-cognition#500 --deepspeed zero2 ...`，与直接写命令行完全等价。

#### 4.3.4 代码实践

**实践目标**：把 README 的一条 `swift sft` 命令改写成 YAML 配置，并通过配置文件调用，验证两者等价。

**操作步骤**：

1. 在工作目录新建 `my_sft.yaml`，内容如下（这是一个最小可训练配置）：

   ```yaml
   ENV:
     CUDA_VISIBLE_DEVICES: '0'
   model: Qwen/Qwen3-4B-Instruct-2507
   tuner_type: lora
   dataset:
     - 'AI-ModelScope/alpaca-gpt4-data-en#500'
   torch_dtype: bfloat16
   output_dir: output
   max_steps: 5
   ```

2. 运行 `swift sft my_sft.yaml --help`（加 `--help` 只为观察展开，不真正训练）。
3. 观察第一行 `run sh:` 打印的命令，确认 `my_sft.yaml` 已被替换为 `--model ... --tuner_type lora --dataset ...` 等参数。

**需要观察的现象**：

- `run sh:` 里已经看不到 `my_sft.yaml` 字样，取而代之的是展开后的 `--key value`。
- 展开后的参数列表，应当与你把 YAML 手写成 `swift sft --model ... --tuner_type lora --dataset ... ` 完全一致。
- `CUDA_VISIBLE_DEVICES` 已通过 ENV 块注入。

**预期结果**：配置文件与命令行两种写法等价；ENV 块成功注入环境变量。

> 若想真正跑训练，去掉 `--help` 即可，但需要约 13GB 显存（参考 README Quick Start）。在你本地未确认资源时，标记为「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：如果我在 shell 里先 `export CUDA_VISIBLE_DEVICES=0`，又在 YAML 的 `ENV` 里写了 `CUDA_VISIBLE_DEVICES: '1'`，最终生效的是哪个？

**参考答案**：生效的是 `0`。因为 `parse_yaml_args` 发现该变量已存在且值不同，会打印一条 warning 并保留 shell 里的原值，不覆盖。这保证了运维环境优先。

**练习 2**：为什么列表类型的值（如 `dataset`）要直接「展开」进参数列表，而不是写成 `--dataset "[a, b]"`？

**参考答案**：因为后端用的是 argparse，ms-swift 的很多参数（`--dataset`、`--target_modules`）被定义成 `nargs='+'` 即「可变多值」，它期望 `--dataset a b c` 这种形式，而不是一个 JSON 数组字符串。展开成多个 token 才能被 argparse 正确消费。

---

## 5. 综合实践

把本讲三个模块串起来，完成一次完整的「命令追踪」。

**任务**：解释下面这条命令从回车到真正进入训练代码的完整路径，并用一份 YAML 配置复现它。

```shell
NPROC_PER_NODE=2 CUDA_VISIBLE_DEVICES=0,1 \
swift sft \
    --model Qwen/Qwen3-4B-Instruct-2507 \
    --tuner_type lora \
    --dataset 'AI-ModelScope/alpaca-gpt4-data-en#500' \
    --output_dir output
```

**要求**：

1. **路由追踪**：说明 `sft` 如何经 `ROUTE_MAPPING` 解析为 `swift/cli/sft.py` 的磁盘路径。
2. **torchrun 判定**：说明因为有 `NPROC_PER_NODE=2`，`use_torchrun()` 返回 True，且 `sft ∈ {pt, sft, rlhf, infer}`，因此走多进程分支，最终命令形如 `python -m torch.distributed.run --nproc_per_node 2 .../sft.py --model ...`。
3. **配置复现**：把上面命令改写成一个 `multigpu_sft.yaml`（含 `ENV` 块写入 `NPROC_PER_NODE` 与 `CUDA_VISIBLE_DEVICES`），并用 `swift sft multigpu_sft.yaml --help` 验证展开结果与原命令一致。

**参考 `multigpu_sft.yaml`（示例代码）**：

```yaml
ENV:
  NPROC_PER_NODE: '2'
  CUDA_VISIBLE_DEVICES: '0,1'
model: Qwen/Qwen3-4B-Instruct-2507
tuner_type: lora
dataset:
  - 'AI-ModelScope/alpaca-gpt4-data-en#500'
output_dir: output
```

**验收标准**：

- 能口头复述「发射器 main.py → 解析子命令 → 判定 torchrun → 展开 yaml → subprocess 重启 sft.py → sft_main」这条链路。
- YAML 展开后的 `run sh:` 与原 shell 命令的多进程形式一致。

> 真正执行 2 卡训练需要 2 张可用 GPU；若仅做链路验证，用 `--help` 观察打印即可，其余标记「待本地验证」。

## 6. 本讲小结

- `swift` 命令由 `setup.py` 的 `console_scripts` 注册到 `swift.cli.main:cli_main`；`megatron` 命令同理指向 `swift.cli._megatron.main:cli_main`。
- `cli_main` 是一个「发射器」：用 `ROUTE_MAPPING` 把子命令映射到脚本文件路径，再用 `subprocess.run` 重新启动该脚本，自己并不干活。
- 是否套 `torchrun` 由 `use_torchrun()`（看 `NPROC_PER_NODE` / `NNODES`）和子命令类型共同决定；只有 `pt/sft/rlhf/infer`（及 megatron 命令）才会走多进程分支。
- `parse_yaml_args` 把 YAML/JSON 配置就地展开为命令行参数，并通过 `ENV` 块注入环境变量（已存在的环境变量优先，不覆盖）。
- `run sh:` 那行打印是排查问题的第一线索，它告诉你框架最终执行的真实命令。
- `swift --help` 报 `KeyError`、`swift sft --help` 正常——根因就在 `--help` 不是 `ROUTE_MAPPING` 的合法子命令。

## 7. 下一步学习建议

理解了分发链路后，建议按以下顺序继续：

- **下一步先学 u1-l5（快速上手 SFT）**：用一条真实命令把「训练 → 推理 → 导出」跑通，建立端到端直觉。
- 之后进入 **u2（参数与配置体系）**：本讲里被 `--model`、`--dataset` 这些参数最终会被 `SftArguments`（一个 dataclass）解析，u2-l1 会讲清楚这套参数类是怎么组织的。
- 对 `parse_yaml_args` 意犹未尽的读者，可以在 u2-l2 里看到配置体系更深的设计（如 `args.json` 的自动回载）。
- 想了解 `sft_main` 之后发生什么的读者，可以先跳到 u5（训练器）的 u5-l4 看 `SwiftSft` 主流程，但建议先完成 u2~u4。
