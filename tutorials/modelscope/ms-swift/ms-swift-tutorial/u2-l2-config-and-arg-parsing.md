# 配置文件与命令行参数解析

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `swift sft config.yaml` 这条命令里，YAML/JSON 文件是如何被「翻译」成一长串命令行参数的。
- 写出一份带 `ENV` 块的配置文件，并准确预测哪些环境变量会被注入、哪些不会。
- 解释训练时落盘的 `args.json` 是怎么产生的，推理/导出时又是怎么被自动读回来，从而实现「训练即所见，推理即所得」。
- 用 `run sh:` 这一行终端输出和 `args.json` 两种手段，验证「配置文件写法」与「命令行写法」是否完全等价。

本讲承接 [u1-l4 CLI 入口与命令分发](u1-l4-cli-entry-and-dispatch.md)（`cli_main` 负责调度）和 [u2-l1 Arguments 数据类体系](u2-l1-arguments-dataclass-system.md)（参数对象如何被组装），专门拆解「**配置文件 ↔ 命令行参数 ↔ args.json**」这条双向数据通路。

## 2. 前置知识

阅读本讲前，建议你已经了解：

- **子命令路由**：`swift sft` 里的 `sft` 会先被 `cli_main` 拆出来当「子命令名」，剩下的部分才是参数（详见 u1-l4）。
- **dataclass 参数体系**：ms-swift 用 `BaseArguments` 这一坨 dataclass 来承载所有参数，命令行参数最终都要对得上 dataclass 里的某个字段（详见 u2-l1）。
- **环境变量**：操作系统的环境变量（如 `CUDA_VISIBLE_DEVICES`、`NPROC_PER_NODE`），可以在 shell 里 `export`，也可以由程序在运行时写入 `os.environ`。
- **YAML / JSON**：两种常见的配置文件格式。YAML 用缩进表示层级、写起来更像人话；JSON 用花括号和引号、更严格。ms-swift 两种都支持。

一个关键直觉：**ms-swift 的配置文件不是「另一种参数体系」，它只是命令行参数的另一种写法。** 框架做的事情，本质上就是把配置文件「原地展开」成一串 `--key value`，再交给和命令行完全相同的解析逻辑。理解了这一点，本讲剩下的内容就都是细节了。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [swift/cli/main.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/cli/main.py) | CLI 入口的「发射器」。其中的 `parse_yaml_args` 负责把配置文件展开为 argv，`cli_main` 负责调度。 |
| [examples/yaml/deepspeed/sft.yaml](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/yaml/deepspeed/sft.yaml) | 真实的 YAML 配置示例，含模型/数据/训练三组参数。 |
| [examples/yaml/megatron/sft.yaml](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/yaml/megatron/sft.yaml) | 真实的带 `ENV` 块的配置示例。 |
| [swift/arguments/base_args/base_args.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/base_args/base_args.py) | 参数基类。其中的 `save_args` 落盘 `args.json`，`load_args_from_ckpt` 回载 `args.json`。 |
| [swift/pipelines/base.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/base.py) | `SwiftPipeline` 基类，调用 `parse_args` 并校验「剩余参数」。 |
| [swift/utils/utils.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/utils/utils.py) | 通用工具，`parse_args` 基于 `HfArgumentParser` 把 argv 解析成参数对象。 |

## 4. 核心概念与源码讲解

### 4.1 YAML/JSON 配置展开为 argv

#### 4.1.1 概念说明

ms-swift 的参数可以有两种等价写法：

- **命令行写法**：`swift sft --model Qwen/Qwen2.5-7B-Instruct --tuner_type lora ...`
- **配置文件写法**：把这些 `--key value` 写进一个 YAML 或 JSON 文件，然后 `swift sft config.yaml`。

这两种写法**最终走的是同一条解析路径**。配置文件只是一个「糖衣」——`parse_yaml_args` 会把文件读进来，把里面的键值对展开成一串 `--key value`，再放回 argv 里。从这一步往后，框架根本不关心参数是来自命令行还是配置文件。

为什么要提供配置文件写法？因为真实训练任务的参数往往有几十个，命令行一长就容易写错、难版本管理。把参数沉淀到 YAML 文件里，就可以和代码一样 `git` 管理、复用、对比。

#### 4.1.2 核心流程

配置文件展开的流程可以概括为下面几步：

```
swift sft config.yaml --extra 1
        │
        ▼
cli_main 拆出子命令 sft，argv = ['config.yaml', '--extra', '1']
        │
        ▼
parse_yaml_args(argv):
  1. 看 argv[0] 后缀：.json → json.load；.yaml/.yml → yaml.safe_load；否则原样返回
  2. 记录 config 文件路径到环境变量 SWIFT_CONFIG_FILE（后面 save_args 要用）
  3. 把 ENV 块单独拎出来处理（见 4.2）
  4. 遍历剩余 key→value，拼成 config_argv：
       标量       → ['--key', 'value']
       list      → ['--key', *list]              # 列表直接铺平
       dict      → ['--key', json.dumps(dict)]   # 字典转 JSON 字符串
  5. 用 config_argv 替换 argv[0]（argv[0:1] = config_argv）
        │
        ▼
argv 变成纯命令行参数：['--model', '...', '--tuner_type', 'lora', ..., '--extra', '1']
        │
        ▼
交给 torchrun/子进程，最终由 parse_args 解析进 dataclass
```

注意第 5 步用的是切片赋值 `argv[0:1] = config_argv`——它**只替换配置文件那个位置**，配置文件后面跟着的额外参数（如 `--extra 1`）会被保留并拼在展开结果后面。这就是 [examples/yaml/deepspeed/infer.sh](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/yaml/deepspeed/infer.sh) 里「配置文件 + 命令行」混合写法能生效的原因。

#### 4.1.3 源码精读

展开逻辑全部集中在一个函数 `parse_yaml_args` 里：

[swift/cli/main.py:L38-L71](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/cli/main.py#L38-L71) —— 读文件、记路径、展开为 argv。

逐段看关键点：

```python
config = None
if argv[0].endswith('.json'):
    with open(argv[0], 'r', encoding='utf-8') as f:
        config = json.load(f)
elif argv[0].endswith('.yaml') or argv[0].endswith('.yml'):
    with open(argv[0], 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)
if config is None:
    return
```

- 它只检查 `argv[0]`（第一个参数）的后缀。如果第一个参数不是 `.json/.yaml/.yml`，`config` 保持 `None`，函数直接 `return`——argv 原封不动，等价于纯命令行调用。
- 这意味着**配置文件必须是紧跟在子命令后面的第一个参数**：`swift sft config.yaml` 对，`swift sft --model xxx config.yaml` 不行（此时 `argv[0]` 是 `--model`，不会被当成配置文件）。

```python
# Used for saving configurations
os.environ['SWIFT_CONFIG_FILE'] = argv[0]
```

- 把配置文件路径记进环境变量 `SWIFT_CONFIG_FILE`。这是给后面 `save_args` 留的钩子——训练结束时会把这个配置文件**复制一份**到 `output_dir`，方便日后复盘（见 4.3.3）。

```python
env = config.pop('ENV', None)        # ENV 块单独处理，见 4.2
...
config_argv = []
for k, v in config.items():
    config_argv.append(f'--{k}')
    if isinstance(v, list):
        config_argv += v             # list：铺平，如 dataset 列表
    else:
        if isinstance(v, dict):
            v = json.dumps(v, ensure_ascii=False)   # dict：转 JSON 字符串
        else:
            v = str(v)               # 标量：转字符串
        config_argv.append(v)
argv[0:1] = config_argv
```

对照一份真实配置 [examples/yaml/deepspeed/sft.yaml:L10-L16](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/yaml/deepspeed/sft.yaml#L10-L16)：

```yaml
dataset:
  - 'AI-ModelScope/alpaca-gpt4-data-zh#500'
  - 'AI-ModelScope/alpaca-gpt4-data-en#500'
  - 'swift/self-cognition#500'
```

这是一个 list，会被展开成 `--dataset AI-ModelScope/alpaca-gpt4-data-zh#500 AI-ModelScope/alpaca-gpt4-data-en#500 swift/self-cognition#500`，和命令行里写 `--dataset a b c` 完全一样。而像 `model: Qwen/Qwen2.5-7B-Instruct` 这种标量，则展开成 `--model Qwen/Qwen2.5-7B-Instruct`。

> 补充：`deepspeed: zero2` 这种简写也会被无差别展开成 `--deepspeed zero2`。至于字符串 `"zero2"` 怎么变成一份真正的 DeepSpeed 配置，是下游 transformers/DeepSpeed 的职责，不在本讲范围。

`parse_yaml_args` 在 `cli_main` 里的调用点：

[swift/cli/main.py:L86-L99](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/cli/main.py#L86-L99) —— 先拆子命令，再展开配置，最后拼成真实命令。

```python
argv = sys.argv[1:]                       # ['sft', 'config.yaml', ...]
method_name = argv[0].replace('_', '-')   # 'sft'
argv = argv[1:]                           # ['config.yaml', ...]
file_path = importlib.util.find_spec(route_mapping[method_name]).origin
parse_yaml_args(argv)                     # 就地展开配置文件
...
print(f"run sh: `{' '.join(args)}`", flush=True)   # 打印最终真实命令
result = subprocess.run(args)
```

最后那行 `print("run sh: ...")` 非常重要：**它打印的就是配置文件展开后框架真正执行的那条命令**。这是验证「配置文件写法 ↔ 命令行写法」是否等价的最直接证据（见 4.1.4 实践）。

展开后的 argv 最终由 `parse_args` 解析进 dataclass：

[swift/utils/utils.py:L174-L183](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/utils/utils.py#L174-L183) —— 用 HfArgumentParser 把 argv 解析成参数对象，并返回「剩余参数」。

```python
def parse_args(class_type, argv=None):
    ...
    parser = HfArgumentParser([class_type])
    ...
    args, remaining_args = parser.parse_args_into_dataclasses(argv, return_remaining_strings=True)
    return args, remaining_args
```

注意它返回了 `remaining_args`——那些**没能在 dataclass 里找到对应字段**的参数。它们会被 `SwiftPipeline._parse_args` 检查：

[swift/pipelines/base.py:L31-L41](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/base.py#L31-L41) —— 有剩余参数就报错（除非 `ignore_args_error=True`）。

```python
args, remaining_argv = parse_args(self.args_class, args)
if len(remaining_argv) > 0:
    if getattr(args, 'ignore_args_error', False):
        logger.warning(f'remaining_argv: {remaining_argv}')
    else:
        raise ValueError(f'remaining_argv: {remaining_argv}')
```

这就是为什么你在 YAML 里把 `lora_ranck` 拼错（多打了个 `c`）会直接报错——它对不上任何 dataclass 字段，落进了 `remaining_argv`。这个机制能在训练开始**之前**就把拼错的参数名拦下来，避免「跑了一晚上才发现参数没生效」。

#### 4.1.4 代码实践

**实践目标**：亲手验证「YAML 配置文件展开后，等价于一条命令行」。

**操作步骤**：

1. 先用命令行写法记下一条最简 sft 命令（不用真跑完训练，只要看 `run sh:` 即可），例如：

   ```bash
   CUDA_VISIBLE_DEVICES=0 swift sft --model Qwen/Qwen2.5-7B-Instruct --tuner_type lora \
     --dataset swift/self-cognition#500 --output_dir output/cli_demo --max_length 2048 \
     --learning_rate 1e-4 --num_train_epochs 1
   ```

   在终端输出里找到 `run sh:` 那一行，复制下来。

2. 新建一个 `demo.yaml`，把上面参数搬进去（key 去掉前面的 `--`）：

   ```yaml
   model: Qwen/Qwen2.5-7B-Instruct
   tuner_type: lora
   dataset:
     - swift/self-cognition#500
   output_dir: output/yaml_demo
   max_length: 2048
   learning_rate: 1e-4
   num_train_epochs: 1
   ```

3. 用配置文件方式启动：

   ```bash
   CUDA_VISIBLE_DEVICES=0 swift sft demo.yaml
   ```

   同样找到 `run sh:` 那一行。

**需要观察的现象**：

- 两次的 `run sh:` 输出，除了 `output_dir`（你故意写成不同值以便区分）和参数**顺序**可能不同外，参数集合应当完全一致。
- 配置文件里的 `dataset` 列表，在 `run sh:` 里被展开成 `--dataset swift/self-cognition#500`。

**预期结果**：两种写法产生的 argv 集合等价。若出现 `remaining_argv` 报错，多半是 YAML 里的 key 拼错了，对照 dataclass 字段修正。

> 说明：本实践侧重「读 `run sh:` 对比」，不需要把训练真正跑完。是否真实可在你本机跑通取决于显存与网络，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：如果把 YAML 写成 `--model: xxx`（key 带了 `--`），展开后会变成什么？会出错吗？

**参考答案**：`config_argv.append(f'--{k}')` 会对任何 key 都无脑加 `--` 前缀，所以会变成 `---model xxx`（三个横杠），从而对不上 dataclass 字段，最终落入 `remaining_argv` 报错。结论：YAML 里的 key **不要**带 `--`。

**练习 2**：`swift sft demo.yaml --lora_rank 16`，YAML 里也写了 `lora_rank: 8`，最终生效的是哪个？

**参考答案**：取决于 argv 顺序。`parse_yaml_args` 只替换 `argv[0]`（配置文件位置），命令行尾部的 `--lora_rank 16` 会被拼在展开结果**之后**。HfArgumentParser 对同一个字段出现多次时的行为是「后出现的覆盖先出现的」，因此命令行尾部写法通常会覆盖配置文件里的值——这也是「配置文件 + 命令行覆盖」组合的标准用法。**待本地验证**覆盖的具体顺序。

---

### 4.2 ENV 块与环境变量注入

#### 4.2.1 概念说明

有些「参数」其实不是 ms-swift 的训练参数，而是**环境变量**——比如控制 CUDA 显存分配策略的 `PYTORCH_CUDA_ALLOC_CONF`、限制多模态图像最大像素数的 `MAX_PIXELS`、设置进程数的 `NPROC_PER_NODE`。这些东西不能写进 `--key value`，只能放进 `os.environ`。

ms-swift 的配置文件专门为此保留了一个特殊顶层 key：`ENV`。它是一个字典，里面的每一项都会在训练开始前被注入到环境变量里。这样你就可以把「训练参数」和「运行环境变量」放在同一个 YAML 里统一管理，而不必在 shell 里另写一长串 `export`。

#### 4.2.2 核心流程

```
parse_yaml_args 读到 config 后：
        │
        ▼
env = config.pop('ENV', None)     # 把 ENV 块从 config 里摘出来（所以它不会被当成训练参数）
        │
        ▼
for k, v in env.items():
    if k 还没在 os.environ 里:      os.environ[k] = str(v)   # 注入
    elif 值和已有的不一样:           打印 warning，保持原值   # 不覆盖
```

关键规则只有一条：**shell 里已经 export 过的环境变量优先，配置文件不会覆盖它。** 这是设计上的安全网——避免一份共享的 YAML 在不同人的机器上意外改掉关键的 shell 配置。

#### 4.2.3 源码精读

ENV 注入的代码紧跟在「读文件」之后，非常短：

[swift/cli/main.py:L53-L59](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/cli/main.py#L53-L59) —— `pop` 出 ENV 块，按「不覆盖」规则注入环境变量。

```python
env = config.pop('ENV', None)
if env:
    for k, v in env.items():
        if k not in os.environ:
            os.environ[k] = str(v)
        elif str(v) != os.environ[k]:
            logger.warning(f'{k} is already set in environment, '
                           f'using `{os.environ[k]}` instead of `{v}`')
```

两个细节值得记住：

- 用的是 `config.pop('ENV', None)`，**弹出**而不是读取。这意味着 `ENV` 不会留在 `config` 里，后面 `for k, v in config.items()` 展开训练参数时不会把它误当成 `--ENV ...`。这也是为什么 `ENV` 必须是配置文件的**顶层** key。
- 值会被 `str(v)` 转成字符串。所以 YAML 里写 `MAX_PIXELS: 1003520`（数字）和 `MAX_PIXELS: '1003520'`（字符串）效果一样。

看一个真实的 ENV 配置 [examples/yaml/megatron/sft.yaml:L1-L5](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/yaml/megatron/sft.yaml#L1-L5)：

```yaml
ENV:
  PYTORCH_CUDA_ALLOC_CONF: 'expandable_segments:True'
  MAX_PIXELS: '1003520'
  VIDEO_MAX_PIXELS: '50176'
  FPS_MAX_FRAMES: '12'
```

它会被注入成四个环境变量。注意它和 `NPROC_PER_NODE` 这类**多进程启动变量**的区别：`NPROC_PER_NODE` 是在 shell 里、在 `swift` 命令**之前**就设好的（见 [examples/yaml/deepspeed/sft.sh:L1-L3](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/yaml/deepspeed/sft.sh#L1-L3)），因为 `use_torchrun()` 要靠它来决定是否拉起多进程；而 `ENV` 块里的变量是在 `parse_yaml_args` 里、即子进程启动**之前**注入的，适合放那些「只要进程跑起来后能读到就行」的变量。

> 小贴士：`PYTORCH_CUDA_ALLOC_CONF`、`MAX_PIXELS` 这类变量之所以写进 `ENV` 而不是命令行，是因为它们不是 dataclass 字段——写进 `--key value` 反而会被 `remaining_argv` 拦下来报错。

#### 4.2.4 代码实践

**实践目标**：观察 ENV 块的「不覆盖」行为。

**操作步骤**：

1. 准备一个 `env_demo.yaml`：

   ```yaml
   ENV:
     MY_SWIFT_TEST_VAR: from_yaml
   model: Qwen/Qwen2.5-7B-Instruct
   output_dir: output/env_demo
   ```

2. 第一次，**不**在 shell 里预设该变量，直接跑（只要能看到 `run sh:` 即可，无需跑完训练）：

   ```bash
   CUDA_VISIBLE_DEVICES=0 swift sft env_demo.yaml
   ```

3. 第二次，先在 shell 里 `export MY_SWIFT_TEST_VAR=from_shell`，再跑同样的命令。

**需要观察的现象**：

- 第一次：`MY_SWIFT_TEST_VAR` 被注入为 `from_yaml`（可在训练日志或后续脚本里读到）。
- 第二次：日志里应出现一行 warning，提示 `MY_SWIFT_TEST_VAR is already set in environment, using 'from_shell' instead of 'from_yaml'`，最终生效值仍是 `from_shell`。

**预期结果**：复现「shell 已有变量优先、YAML 不覆盖」的规则。**待本地验证** warning 文案是否逐字一致。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `ENV` 必须用 `config.pop` 摘出来，而不是像别的 key 一样直接展开？

**参考答案**：如果 `ENV` 留在 config 里，它会被展开成 `--ENV {...}`，而 dataclass 里并没有 `ENV` 字段，于是落入 `remaining_argv` 报错。`pop` 既取出了值做环境变量注入，又让它不参与参数展开，一举两得。

**练习 2**：把 `NPROC_PER_NODE: 2` 写进 `ENV` 块，预期能让训练用两张卡吗？

**参考答案**：**通常不行**。`use_torchrun()` 在 `cli_main` 里判断是否多进程时，`parse_yaml_args` 虽然在其**之前**被调用、确实会注入该变量，但更稳妥、更常见的做法是像 [sft.sh](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/yaml/deepspeed/sft.sh#L1-L3) 那样在 shell 行首设置 `NPROC_PER_NODE=2`。建议遵循官方示例把分布式变量放在 shell。**待本地验证** `ENV` 块里写 `NPROC_PER_NODE` 是否真的能触发多进程。

---

### 4.3 args.json 持久化与回载

#### 4.3.1 概念说明

前两个模块讲的是「**进**」——参数怎么从配置文件/命令行流进训练进程。这个模块讲「**出**」和「再进」：

- 训练开始后，ms-swift 会把当前这次运行的全部参数**序列化成一个 `args.json`**，保存到 `output_dir` 里。
- 之后做 `swift infer` 或 `swift export` 时，只要指向训练产物（checkpoint 目录），框架会**自动读回这个 `args.json`**，把模型类型、模板、系统提示、量化方式等关键配置恢复出来。

这就是「训练即所见，推理即所得」的实现原理：你训练时用了什么模板、什么系统提示、什么量化，推理时不用再重述一遍，框架从 `args.json` 里自动还原。此外，如果你用配置文件跑训练，那份配置文件也会被**复制一份**进 `output_dir`，方便日后复盘。

#### 4.3.2 核心流程

```
训练阶段（save_args）：
  self.__dict__  ──check_json_format──▶  args.json  （写入 output_dir）
  若环境变量 SWIFT_CONFIG_FILE 存在      ──shutil.copy──▶  把 config.yaml 也复制进 output_dir

推理/导出阶段（load_args_from_ckpt）：
  读 args.json → old_args（一个 dict）
  对每个 (key, old_value)：
     old_value 为 None          → 跳过
     key 在 force_load_keys 里  → 无条件覆盖当前值（强制回载）
     key 在 load_keys 里且当前为空 → 回载（当前已有值则保留）
     load_data_args=True 且 key 属于 data_keys → 回载数据相关参数
```

回载分三档优先级，可以记成一个简单的不等式：

\[
\text{force\_load\_keys} \;>\; \text{data\_keys（需 load\_data\_args 开关）} \;>\; \text{load\_keys（仅当前为空时才填）}
\]

即：`force_load_keys` 最强势、无条件覆盖；`load_keys` 最克制、只有当前没值时才补；`data_keys` 介于两者之间、受 `load_data_args` 开关控制。

#### 4.3.3 源码精读

**落盘：`save_args`**

[swift/arguments/base_args/base_args.py:L303-L313](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/base_args/base_args.py#L303-L313) —— 把参数对象序列化成 `args.json`，并顺便把配置文件复制进 `output_dir`。

```python
def save_args(self, output_dir=None) -> None:
    if is_master():                       # 只有主进程写，避免多卡冲突
        output_dir = output_dir or self.output_dir
        os.makedirs(output_dir, exist_ok=True)
        fpath = os.path.join(output_dir, 'args.json')
        ...
        with open(fpath, 'w', encoding='utf-8') as f:
            json.dump(check_json_format(self.__dict__), f, ensure_ascii=False, indent=2)
        config_file = os.getenv('SWIFT_CONFIG_FILE')
        if config_file:
            shutil.copy(config_file, output_dir)
```

两个看点：

- `check_json_format(self.__dict__)` 会把 dataclass 实例的整个属性字典转成「可 JSON 序列化」的结构（把不可序列化的对象转成字符串等），这也是 `args.json` 里能看到几乎所有参数的原因。
- 末尾的 `shutil.copy` 正是 4.1 里埋的伏笔：只要你是用配置文件跑的（`SWIFT_CONFIG_FILE` 非空），那份 YAML/JSON 就会被复制进产物目录。

`sft_main` 在拿到数据集、准备真正训练前就会调用它：

[swift/pipelines/train/sft.py:L167](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/train/sft.py#L167) —— `args.save_args()`，确保训练一启动就落盘参数。

**回载：`load_args_from_ckpt`**

[swift/arguments/base_args/base_args.py:L246-L301](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/base_args/base_args.py#L246-L301) —— 读回 `args.json`，按三档优先级回填参数。

核心数据结构是三张「key 名单」：

```python
force_load_keys = ['tuner_type', 'task_type', 'bnb_4bit_quant_type', 'bnb_4bit_use_double_quant']
load_keys = ['external_plugins', 'model', 'model_type', 'model_revision', 'torch_dtype',
             'attn_impl', ..., 'template', 'system', 'truncation_strategy', ...]
data_keys = list(f.name for f in fields(DataArguments))   # 所有数据相关字段
```

回填逻辑：

```python
for key, old_value in old_args.items():
    if old_value is None:
        continue
    if key in force_load_keys or self.load_data_args and key in data_keys:
        setattr(self, key, old_value)            # 强制 / 数据回载
    value = getattr(self, key, None)
    if key in load_keys and (value is None or isinstance(value, (list, tuple)) and len(value) == 0):
        setattr(self, key, old_value)            # 仅当前为空时才补
```

理解这份名单的设计意图：

- `force_load_keys`（强制回载）：`tuner_type`、`task_type`、量化方式——这些一旦和训练时不一致，推理就会出错（比如训练时是 `causal_lm` 你却按 `seq_cls` 推理），所以**必须**用训练时的值，无条件覆盖命令行。
- `load_keys`（按需回载）：`model`、`template`、`system`、`torch_dtype` 等——推理时**如果你没显式指定**，就用训练时的值；**如果你显式指定了**，就以你的为准。这就是「免配置复用」又不「锁死」的平衡点。
- `data_keys`（条件回载）：默认**不**回载数据参数（`load_data_args=False`），只有你想在验证集上推理（`--load_data_args true`）时才会把训练用的数据配置也搬过来。

**何时触发回载**：在 `_init_ckpt_dir` 里，只要定位到了 checkpoint 目录且 `load_args=True`：

[swift/arguments/base_args/base_args.py:L236-L244](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/base_args/base_args.py#L236-L244) —— 根据当前 `--model`/`--adapters` 定位 checkpoint 目录，并在 `load_args` 为真时回载参数。

```python
def _init_ckpt_dir(self, adapters=None):
    ...
    self.ckpt_dir = get_ckpt_dir(model, adapters)
    if self.ckpt_dir and self.load_args:
        self.load_args_from_ckpt()
```

`load_args` 这个字段的默认值很讲究（[base_args.py:L69-L73](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/base_args/base_args.py#L69-L73)）：推理/导出时默认 `True`（自动回载），训练时默认 `False`（避免你resume训练时被旧参数意外覆盖）。另一个相关开关是 `from_pretrained`，它走的是「彻底以 args.json 为准」的路径：

[swift/arguments/base_args/base_args.py:L224-L234](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/base_args/base_args.py#L224-L234) —— `from_pretrained` 直接从 checkpoint 构造一个参数对象，`load_data_args` 置真，回载 `args.json`。

```python
@classmethod
def from_pretrained(cls, checkpoint_dir: str):
    self = super().__new__(cls)
    self.load_data_args = True
    self.ckpt_dir = checkpoint_dir
    self.load_args_from_ckpt()
    ...
```

#### 4.3.4 代码实践

**实践目标**：亲眼看到 `args.json` 的产生与自动回载。

**操作步骤**：

1. 跑一次极小的 sft（哪怕只跑几十步就停），让它产出 checkpoint：

   ```bash
   CUDA_VISIBLE_DEVICES=0 swift sft --model Qwen/Qwen2.5-7B-Instruct --tuner_type lora \
     --dataset swift/self-cognition#200 --output_dir output/args_demo \
     --system "You are a helpful assistant." --max_steps 20
   ```

2. 训练结束后，打开 `output/args_demo/args.json`，找到 `system`、`template`、`tuner_type` 三个字段，记下它们的值。

3. 用**最简参数**做推理，**不**指定 `--system`、`--template`：

   ```bash
   CUDA_VISIBLE_DEVICES=0 swift infer --adapters output/args_demo/checkpoint-XXX
   ```

**需要观察的现象**：

- 推理日志里会打印一行 `Successfully loaded .../args.json.`，说明回载触发了。
- 即使你没在命令行写 `--system`，推理时用的系统提示仍是训练时的 `You are a helpful assistant.`——这就是 `load_keys` 的「按需回载」在起作用。
- 如果你在命令行显式加 `--system "别的提示"`，则命令行值生效、覆盖回载值。

**预期结果**：`args.json` 是训练参数的权威快照，推理/导出自动从中恢复关键配置。若你用的是配置文件跑的训练，再去 `output/args_demo/` 里找一份被复制进去的同名 YAML。**待本地验证**具体 checkpoint 目录名（`checkpoint-XXX` 中的步数）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `tuner_type` 在 `force_load_keys` 里，而 `system` 在 `load_keys` 里？

**参考答案**：`tuner_type` 决定了 adapter 的结构（如 LoRA vs full），推理时和训练时**必须**一致，否则权重都对不上，所以强制覆盖、不给用户改错的机会。而 `system` 只是对话风格，用户在推理时**可能**想临时换一个，所以「没指定就用训练的、指定了就用指定的」，给了灵活性。

**练习 2**：训练时 `swift sft` 默认会不会从旧 checkpoint 的 `args.json` 回载参数？

**参考答案**：默认不会。训练场景下 `load_args` 默认 `False`（见 [base_args.py:L102](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/base_args/base_args.py#L102)），目的是避免 resume 训练时被旧参数意外覆盖当前命令行的意图。推理/导出时才默认开启回载。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个端到端小任务。

**任务**：为一条 sft 命令编写等价的带 `ENV` 块的 YAML 配置，用配置文件方式运行，并从产物中读回参数。

**步骤**：

1. 选一条你想固化的命令行 sft（可用 4.1.4 里的命令）。
2. 把它改写成 `final.yaml`，要求：
   - 模型、tuner、数据、训练超参各组齐全（参考 [examples/yaml/deepspeed/sft.yaml](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/yaml/deepspeed/sft.yaml) 的分组注释风格）。
   - 顶层加一个 `ENV` 块，至少包含 `PYTORCH_CUDA_ALLOC_CONF: 'expandable_segments:True'`（参考 [examples/yaml/megatron/sft.yaml:L1-L5](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/yaml/megatron/sft.yaml#L1-L5)）。
   - 故意把某个 key 拼错（如 `lora_ranck`），观察 `remaining_argv` 报错，然后改回。
3. 运行 `CUDA_VISIBLE_DEVICES=0 swift sft final.yaml`，记录 `run sh:` 输出，与第 1 步的命令行逐参数对比，确认集合一致。
4. 训练（或跑到产出 checkpoint 即可）后：
   - 打开 `output/args.json`，确认 `system`、`template`、`tuner_type` 等已落盘。
   - 确认 `final.yaml` 被复制进了 `output/` 目录（因为 `SWIFT_CONFIG_FILE` 生效）。
5. 用 `swift infer --adapters output/.../checkpoint-XXX`（不带 `--system`）验证 `args.json` 自动回载。

**验收标准**：

- `run sh:` 与原命令行参数集合一致；
- `output/` 下同时存在 `args.json` 和你的 `final.yaml`；
- 推理时即使省略 `--system`，行为仍与训练时一致。

> 说明：第 3–5 步能否在你本机完整跑通，取决于 GPU 显存与能否下载模型/数据集，**待本地验证**。若资源不足，可只做第 2、3 步的「写 YAML + 读 `run sh:`」部分，同样能验证配置展开逻辑。

## 6. 本讲小结

- **配置文件只是命令行的另一种写法**：`parse_yaml_args` 把 YAML/JSON 就地展开成一串 `--key value`，list 铺平、dict 转 JSON 字符串，之后走与命令行完全相同的解析路径。
- **配置文件必须紧跟子命令**：只有 `argv[0]` 是 `.json/.yaml/.yml` 才会被当成配置；它后面的额外命令行参数会被保留，支持「配置文件 + 命令行覆盖」混合用法。
- **`ENV` 块用于注入环境变量**，用 `pop` 摘出以免污染参数展开；规则是「shell 已有的变量优先、YAML 不覆盖」，冲突时只打 warning。
- **`run sh:` 是排查问题的第一线索**：它打印的就是配置展开后框架真正执行的那条命令。
- **`remaining_argv` 是参数名拼写错误的防线**：对不上 dataclass 字段的参数会在训练开始前就报错。
- **`args.json` 是「训练即所见，推理即所得」的核心**：训练时 `save_args` 落盘（顺带把配置文件复制进 `output/`），推理/导出时 `load_args_from_ckpt` 按 `force_load_keys` / `load_keys` / `data_keys` 三档优先级自动回载。

## 7. 下一步学习建议

本讲讲清了「参数怎么进、怎么存、怎么读回来」。接下来建议：

- 进入 **u3 模型与模板** 单元，看看被解析进 dataclass 的 `model`、`template` 等参数，在 `get_model_processor` / `get_template` 里是如何真正驱动模型加载和对话格式化的。
- 如果你对分布式启动感兴趣，可以提前跳到 **u9-l1 分布式训练基础**，那里会详细讲 `NPROC_PER_NODE` / `NNODES` 这些环境变量如何与 `use_torchrun()` 配合拉起多进程。
- 想看参数对象内部到底有哪些字段，复习 **u2-l1 Arguments 数据类体系**，对照 `DataArguments` / `ModelArguments` / `TemplateArguments` 的字段定义，你会更清楚 `args.json` 里每一项的来源。
