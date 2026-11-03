# 启动与诊断：deepspeed 命令与 ds_report

## 1. 本讲目标

前三讲我们完成了三件事：认识 DeepSpeed 的定位（u1-l1）、画出仓库代码地图（u1-l2）、理解安装与算子编译（u1-l3）。现在假设你已经 `pip install deepspeed` 成功——**按下回车执行 `deepspeed ...` 的那一瞬间，到训练进程真正跑起来之间，发生了什么？** 本讲就把这段"从命令行到进程"的链路拆开看。

学完本讲，你应该能够：

1. 说出 `bin/deepspeed` 这个 CLI 入口脚本如何把控制权转交给 `deepspeed.launcher.runner.main()`，以及它为什么只有 6 行。
2. 读懂 `runner.py` 的参数分发：`--hostfile`、`--include`/`--exclude`、`--num_nodes`/`--num_gpus` 如何被解析成一张"机器 × 卡位"的资源表。
3. 读懂 `launch.py` 如何在本节点上孵化出 N 个训练进程、注入哪些分布式环境变量、拼出什么样的进程命令行。
4. 掌握单机（`--num_gpus`）与多机（hostfile、include/exclude）两种启动写法，并能与 `torchrun` 等价对照。
5. 用 `ds_report`（`env_report.py`）对环境做一次完整体检，并读懂报告里每一栏的含义。

## 2. 前置知识

阅读本讲前，建议了解以下概念（不熟悉也没关系，下面用两三句话解释）：

- **CLI（Command-Line Interface，命令行入口）**：你在终端敲的 `deepspeed`、`ds_report` 其实是可执行脚本文件。pip 安装包时可以把这些脚本复制到环境的 `bin/` 目录（Linux）或 `Scripts/`（Windows），从而进入 `PATH`，终端里就能直接按名字调用。
- **argparse 与 REMAINDER**：Python 标准库的命令行解析器。`parser.parse_args()` 会把 `--num_gpus 1` 这类"启动器参数"解析成对象属性；而 `nargs=REMAINDER` 表示"剩下的所有参数原样收进来"，用来装**用户训练脚本自己的参数**——两类参数靠这个机制隔离开。
- **子进程与环境变量**：Python 用 `subprocess.Popen(cmd, env=...)` 启动子进程。分布式训练的关键信息（`RANK`、`LOCAL_RANK`、`WORLD_SIZE`、`MASTER_ADDR`、`MASTER_PORT`）都是通过**环境变量**传给每个训练进程的，训练框架（PyTorch 分布式）再从环境变量里读出来组网。
- **MPI 风格 hostfile**：高性能计算领域描述"哪些机器可用"的文本格式，每行一条：`主机名 slots=卡数`。DeepSpeed 沿用了这个约定。
- **global rank 与 local rank**：多机训练中每个进程有全局编号 `RANK`（从 0 开始跨机器连续编号）；同时有本机内编号 `LOCAL_RANK`（每台机器从 0 重新数）。两者换算关系见 4.3.2。
- **base64 编码传参**：把一个字典序列化成 JSON 再 base64 编码，就能安全地塞进另一条命令行的字符串参数里。DeepSpeed 用它把整张资源表传给下一级启动器。
- **torchrun**：PyTorch 官方的分布式启动器（`torch.distributed.run`）。它做的事与 DeepSpeed 启动器高度重叠：设环境变量、按卡数孵化进程。本讲把它当作"对照物"，帮你理解 DeepSpeed 启动器多做了什么。

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| [bin/deepspeed](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/bin/deepspeed#L1-L6) | `deepspeed` 命令的入口脚本：6 行薄壳，转发到 `launcher.runner.main()` |
| [bin/ds_report](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/bin/ds_report#L1-L6) | `ds_report` 命令的入口脚本：转发到 `env_report.cli_main()` |
| [deepspeed/launcher/runner.py](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/launcher/runner.py#L46-L209) | 启动器**前端**：解析 CLI 参数、解析 hostfile/include/exclude、决定单机或多机路径 |
| [deepspeed/launcher/launch.py](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/launcher/launch.py#L135-L289) | 启动器**节点级后端**：在单个节点上孵化全部训练进程并注入环境变量 |
| [deepspeed/launcher/multinode_runner.py](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/launcher/multinode_runner.py#L19-L129) | 多机后端：pdsh/OpenMPI/Slurm 等六种远端启动适配器（本讲只看骨架，细节留给 u3-l4） |
| [deepspeed/env_report.py](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/env_report.py#L31-L164) | `ds_report` 的全部实现：算子状态报告 + 环境信息报告 |
| [deepspeed/constants.py](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/constants.py#L12-L24) | 全局常量：默认端口 29500、`CROSS_RANK`/`CROSS_SIZE` 环境变量名 |
| [deepspeed/launcher/constants.py](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/launcher/constants.py#L6-L12) | 启动器后端名字常量（`pdsh`、`openmpi`、`slurm`…） |
| [docs/_tutorials/getting-started.md](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/docs/_tutorials/getting-started.md#L173-L227) | 官方文档：hostfile 与 include/exclude 的用户侧写法 |

一条链路先记在心里：

```text
终端: deepspeed --num_gpus 1 train.py --foo bar
  └─ bin/deepspeed            (薄壳)
      └─ runner.main()        (前端: 解析资源 → 决定路径)
          ├─ 单机: subprocess → python -u -m deepspeed.launcher.launch --world_info=...
          │            └─ launch.main()   (节点级: 孵化 N 个训练进程)
          │                ├─ python -u train.py --local_rank=0 --foo bar   (rank 0)
          │                └─ ...
          └─ 多机: PDSHRunner/OpenMPIRunner/... → 远端再执行 launch.main()
```

## 4. 核心概念与源码讲解

### 4.1 CLI 入口脚本：6 行 Python 的"薄壳"

#### 4.1.1 概念说明

`deepspeed` 在终端里看起来像个功能复杂的命令，但它的入口文件只有 6 行。这是刻意的设计模式——**薄壳入口（thin wrapper）**：

- 入口脚本只做一件事：把"命令名"翻译成"Python 模块里的函数调用"。
- 所有真正的逻辑放在可测试、可复用的库模块里（`deepspeed.launcher.runner`、`deepspeed.env_report`）。
- 好处：同一个逻辑既能被终端用，也能被测试代码 `from deepspeed.launcher.runner import main` 直接调用，还能被 `python -m` 方式调用（4.1.4 会验证）。

`bin/` 目录下还有一组兄弟命令：`ds`（旧名兼容）、`ds_ssh`、`ds_bench`、`ds_nvme_tune`、`dsr` 等，它们全部是同样风格的薄壳。

#### 4.1.2 核心流程

```text
pip install deepspeed
  → setup.py 的 scripts=[...] 把 bin/ 下脚本装入环境 bin/
终端敲 deepspeed ...
  → 操作系统按 PATH 找到该脚本，用 python 解释器执行
  → from deepspeed.launcher.runner import main; main()
  → 控制权进入启动器前端（4.2）
```

#### 4.1.3 源码精读

[bin/deepspeed:L1-L6](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/bin/deepspeed#L1-L6) —— `deepspeed` 命令的全部内容：shebang 行声明用 python3 执行，import 启动器前端的 `main`，在 `__main__` 保护下调用。这就是整个命令。

[bin/ds_report:L1-L6](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/bin/ds_report#L1-L6) —— `ds_report` 同样是薄壳，转发到 `env_report.cli_main()`（见 4.4）。

[setup.py:L292-L297](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/setup.py#L292-L297) —— 安装期如何让这些脚本"上 PATH"：`setup()` 的 `scripts=` 参数按平台传入不同的脚本列表，Windows 用 `.bat`/`ds`，其余平台装入 `bin/deepspeed`、`bin/ds_report` 等。这呼应了 u1-l3 讲过的安装流程——脚本与包代码在 pip 安装时一起落地。

#### 4.1.4 代码实践

1. **实践目标**：确认你终端里的 `deepspeed` 命令就是这个薄壳，并验证"薄壳 = 模块入口"的等价性。
2. **操作步骤**：
   ```bash
   which deepspeed          # 找到命令实际路径（通常在虚拟环境的 bin/ 下）
   cat $(which deepspeed)   # 应看到与 bin/deepspeed 一致的 6 行
   deepspeed --help | head -20
   python -m deepspeed.launcher.runner --help | head -20   # 直接以模块方式调用前端
   ```
3. **需要观察的现象**：两条 `--help` 的输出应当完全一致——因为它们最终执行的是同一个 `runner.py` 里的 `parse_args()`。
4. **预期结果**：帮助文本列出 `--hostfile`、`--include`、`--exclude`、`--num_gpus` 等参数（正是 4.2 要精读的那批）。若 `python -m` 方式报 `No module named deepspeed`，说明当前解释器不是安装了 DeepSpeed 的那个环境。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `bin/deepspeed` 里要有 `if __name__ == '__main__':` 这层判断？
**答案**：该脚本也可能被当作模块导入（例如打包工具在安装时读取它）。加判断后，只有"作为脚本直接执行"时才调用 `main()`，被导入时不会产生副作用（不会真的去启动训练）。

**练习 2**：`bin/` 下还有 `ds_report.bat`，为什么 Windows 需要单独一个 `.bat`？
**答案**：Linux/macOS 靠脚本首行的 shebang（`#!/usr/bin/env python3`）决定用什么解释器执行；Windows 不支持 shebang，需要 `.bat` 批处理文件来显式调用 python。`setup.py:L292-L293` 正是按 `sys.platform == "win32"` 区分了两套脚本列表。

### 4.2 runner.py：参数分发与资源池解析（启动器前端）

#### 4.2.1 概念说明

你在命令行敲的参数其实分两类，`runner.py` 用 argparse 把它们切干净：

- **启动器参数**：`--num_gpus`、`--hostfile`、`--include`… 描述"**在哪张资源表上跑**"，由 runner 消费。
- **用户脚本参数**：`train.py --epochs 3 --lr 1e-4` 里的后半部分，runner 不理解也不需要理解，用 `REMAINDER` 原样收集，最终原样追加到训练进程命令行末尾。

runner 的核心产物是一张**资源表（active_resources）**：`OrderedDict{主机名: [卡位列表]}`。三股力量共同决定这张表：

1. **hostfile**：声明资源池（有哪些机器、各几张卡）；
2. **`--include`/`--exclude`**：在池内挑子集或剔除部分卡（两者互斥）；
3. **`--num_nodes`/`--num_gpus`**：截取前 N 台机器 / 每台只留前 M 张卡（与 include/exclude 互斥）。

没有 hostfile 时（最常见的情况：单机），runner 自动回退为 `localhost = 本机卡数`。

#### 4.2.2 核心流程

```text
main()
 1. fetch_hostfile(args.hostfile)          # 无文件 → None，仅告警
 2. 尊重 CUDA_VISIBLE_DEVICES（单机且未用资源参数时）
 3. 无资源池 → 回退 {localhost: device_count()}
 4. parse_inclusion_exclusion(池, include, exclude)   # 过滤
 5. apply_num_nodes_and_gpus(..., num_nodes, num_gpus) # 截取
 6. encode_world_info(资源表)              # dict → JSON → base64
 7. 分支：
    a. 单机/单主机 → 拼命令 [python, -u, -m, deepspeed.launcher.launch,
                              --world_info=..., master_addr/port ...] + 用户脚本
    b. 多主机     → 按 --launcher 选 PDSH/OpenMPI/Slurm/... Runner，
                    由它负责 ssh 到每台机器再执行 launch（见 4.2.3 末尾）
 8. subprocess.Popen(cmd) 并等待退出码，非零则透传退出
```

全局编号与进程总数的推导（4.3 会用到）：

\[ \text{world\_size} = \sum_{h \in \text{hosts}} |slots_h| \]

\[ \text{global\_rank}(h_i, slot_j) = \sum_{k<i} |slots_{h_k}| + j \]

#### 4.2.3 源码精读

**参数定义**——[runner.py:L51-L95](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/launcher/runner.py#L51-L95) 定义了最常用的资源参数：`-H/--hostfile` 默认值是 `/job/hostfile`（DLTS 集群约定的路径，见 [runner.py:L35](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/launcher/runner.py#L35)）；`-i/--include` 与 `-e/--exclude` 的格式是 `NODE_SPEC[@NODE_SPEC ...]`，其中 `NODE_SPEC = 主机名[:卡位,卡位...]`，省略 `:卡位` 表示该机全部卡；`--num_nodes` 默认 `-1`（不生效）；`--num_gpus` 有别名 `--num_accelerators`，含义是"每台机器最多用前 N 张卡"。

[runner.py:L174-L177](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/launcher/runner.py#L174-L177) —— 位置参数 `user_script` 加 `REMAINDER` 型 `user_args`：这就是"启动器参数与用户参数"的分界线。命令行里用户脚本名之后的everything 都会进 `user_args`。

**hostfile 解析**——[runner.py:L225-L254](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/launcher/runner.py#L225-L254)：`_parse_hostfile` 用正则 `^(\S+)\s+slots=(\d+)`（[L229](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/launcher/runner.py#L229)）逐行匹配 `worker-0 slots=4` 样式的条目，跳过注释与空行；同一主机出现两次或格式错误都直接抛 `ValueError`，结果收进 `OrderedDict`（保持顺序，因为后续 rank 分配与顺序有关）。

**include/exclude 过滤**——[runner.py:L292-L370](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/launcher/runner.py#L292-L370)：`parse_resource_filter` 先做互斥检查（[L306-L308](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/launcher/runner.py#L306-L308)：两者同时给出即报错）；include 从零构建子表，exclude 则深拷贝原表再逐项删除；带卡位的写法会校验"主机存在、卡位存在"；最后去重、删掉空主机，并按原 hostfile 顺序重排。注意 [runner.py:L373-L382](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/launcher/runner.py#L373-L382) 的 `parse_inclusion_exclusion` 会先把 `slots=N` 展开成 `[0..N)` 再过滤——include/exclude 校验针对的是真实资源，而不是字符串自洽。

**num_nodes/num_gpus 截取**——[runner.py:L385-L406](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/launcher/runner.py#L385-L406)：`num_nodes > 0` 只保留前 N 台主机；`num_gpus > 0` 把每台主机的卡位**替换**为 `[0, num_gpus)`（注意是替换不是裁剪——所以 `--num_gpus` 的语义固定是"前 N 张卡"）。

**main 主流程**——[runner.py:L446](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/launcher/runner.py#L446) 读 hostfile（找不到只告警不报错，[runner.py:L212-L216](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/launcher/runner.py#L212-L216)）；[runner.py:L448-L460](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/launcher/runner.py#L448-L460) 在单机且没写资源参数时尊重 `CUDA_VISIBLE_DEVICES`（自动翻译成 `--include=localhost:...`）；[runner.py:L462-L464](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/launcher/runner.py#L462-L464) 强制 `num_nodes/num_gpus` 与 `include/exclude` 互斥。

[runner.py:L466-L474](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/launcher/runner.py#L466-L474) —— **单机回退**：没有 hostfile 时用加速器抽象层（u3-l1 的主角，这里先见一面）查询本机卡数，资源表变成 `{localhost: device_count}`，并强制 `master_addr=127.0.0.1`、关闭多机模式。若 `device_count() == 0` 直接抛 `Unable to proceed, no GPU resources available`——这是"CPU 机器上跑 `deepspeed` 报没卡"的报错来源（解决办法见 4.3.4）。

[runner.py:L483-L493](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/launcher/runner.py#L483-L493) —— 多机模式下先做一次**免密 ssh 连通性检查**（`ssh -o PasswordAuthentication=no 主机 hostname`），失败时给出很有辨识度的提示："单机请删掉 hostfile，多机请配好免密"。[runner.py:L495-L515](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/launcher/runner.py#L495-L515) 在未指定 `--master_addr` 时通过 `hostname -I` 自动探测第一台主机的 IP。

**单机路径的命令拼装**——[runner.py:L537-L564](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/launcher/runner.py#L537-L564)：判定多机的条件是 `(force_multi 或 主机数>1) 且 未用 --no_ssh`。单机时拼出：

```python
[sys.executable, "-u", "-m", "deepspeed.launcher.launch",
 f"--world_info={world_info_base64}",
 f"--master_addr={...}", f"--master_port={...}", ...] + [user_script] + user_args
```

注意 [runner.py:L409-L412](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/launcher/runner.py#L409-L412) 的 `encode_world_info`：资源表 `{"localhost": [0,1]}` → JSON 字符串 → base64，作为一条命令行参数传给 launch 模块。

**多机路径**——[runner.py:L429-L436](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/launcher/runner.py#L429-L436) 的 `LAUNCHER_CLASSES` 把 `--launcher` 的六个取值（常量定义在 [deepspeed/launcher/constants.py:L6-L12](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/launcher/constants.py#L6-L12)）映射到具体 Runner 类；[runner.py:L598-L611](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/launcher/runner.py#L598-L611) 还会把以 `MLFLOW/PYTHON/MV2/UCX` 开头及加速器要求导出的环境变量、加上 `~/.deepspeed_env` 文件里的键值对，一并带去远端节点。骨架见 [multinode_runner.py:L19-L62](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/launcher/multinode_runner.py#L19-L62)（抽象基类 `MultiNodeRunner`，约定 `backend_exists()` 与 `get_cmd()` 两个接口）；以默认的 [PDSHRunner:L88-L129](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/launcher/multinode_runner.py#L88-L129) 为例，它拼出 `pdsh -w 主机列表 ... python -u -m deepspeed.launcher.launch --node_rank=%n ...`——`%n` 是 pdsh 的占位符，会被替换成目标主机在列表中的序号，这样每台机器拿到的 `--node_rank` 各不相同。细节展开留给 u3-l4。

**收尾**——[runner.py:L618-L640](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/launcher/runner.py#L618-L640)：`logger.info(f"cmd = {...}")` 打印最终命令（**这是实践环节要抓的第一条日志**），`Popen` 启动后 `wait()`，子进程退出码非零时原样 `sys.exit` 透传——所以训练脚本崩了，shell 里看到的返回码是真实的。

#### 4.2.4 代码实践

1. **实践目标**：不经真机启动，纯本地验证资源解析逻辑——给定 hostfile 文本与 include/exclude 字符串，推出资源表。
2. **操作步骤**（示例代码，可直接保存为 `parse_drill.py` 在装有 deepspeed 的环境运行）：
   ```python
   # 示例代码：直接调用 DeepSpeed 内部解析函数做推演
   from deepspeed.launcher.runner import (_parse_hostfile, parse_inclusion_exclusion,
                                          apply_num_nodes_and_gpus)
   hostfile_text = ["worker-0 slots=4\n", "worker-1 slots=2\n"]
   pool = _parse_hostfile(hostfile_text)                # → OrderedDict(worker-0:4, worker-1:2)
   print("pool =", pool)
   inc = parse_inclusion_exclusion(pool, inclusion="worker-1@worker-0:0,2", exclusion="")
   print("after include =", inc)
   trim = apply_num_nodes_and_gpus(inc, num_nodes=1, num_gpus=-1)
   print("after num_nodes=1 =", trim)
   ```
   注意：`parse_args` 里 `num_nodes/num_gpus` 与 `include/exclude` 互斥，但这两个纯函数本身可单独调用，便于推演。
3. **需要观察的现象**：第一行输出 `OrderedDict([('worker-0', 4), ('worker-1', 2)])`；第二行应只剩 `worker-0` 的 `[0, 2]` 和 `worker-1` 的全部卡（展开为 `[0, 1]`）。
4. **预期结果**：与 [docs/_tutorials/getting-started.md:L213-L227](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/docs/_tutorials/getting-started.md#L213-L227) 中官方示例的语义一致。若想再验互斥行为，可运行 `deepspeed --num_gpus 1 --include localhost:0 x.py`，应看到 `ValueError: Cannot specify num_nodes/gpus with include/exclude`（[runner.py:L462-L464](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/launcher/runner.py#L462-L464)）。

#### 4.2.5 小练习与答案

**练习 1**：hostfile 写成 `worker-0 slots=4` 和 `worker-0 slots=2` 两行会发生什么？
**答案**：`_parse_hostfile` 检测到同一主机重复出现，抛 `ValueError: Hostfile contains multiple entries for worker-0`（[runner.py:L242-L245](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/launcher/runner.py#L242-L245)）。

**练习 2**：`--num_gpus 2` 在"单机 8 卡、无 hostfile"时最终资源表是什么？
**答案**：先回退为 `{localhost: 8}`（[runner.py:L466-L474](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/launcher/runner.py#L466-L474)），再经 `apply_num_nodes_and_gpus` 把卡位替换为 `[0, 1]`，即 `{"localhost": [0, 1]}`，world_size=2，只会孵化 2 个训练进程。

**练习 3**：为什么资源表要用 `OrderedDict` 而不是普通 `dict`？
**答案**：全局 rank 按"主机在表中的顺序 + 卡位"连续编号（见 4.3.2 公式）。Python 3.7+ 的普通 dict 虽然也保序，但代码显式使用 `OrderedDict` 表达"顺序是 rank 分配的输入"这一契约，且 [runner.py:L363-L368](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/launcher/runner.py#L363-L368) 在过滤后要按原 hostfile 顺序重排，保证所有节点、以及重跑时 rank 映射稳定可复现。

### 4.3 launch.py：单节点进程孵化器（参数分发到每个 rank）

#### 4.3.1 概念说明

`launch.py` 运行在**每一台节点上**（单机时只有本机；多机时由 pdsh 等远端触发），职责一句话：**把本节点的 N 个卡位变成 N 个训练进程，并把分布式"户口"（环境变量）发给每个进程**。

它与 runner 的分工：runner 管"全局资源视角"（哪些机器哪些卡、master 地址）；launch 管"本节点视角"（我拿到 `--node_rank`，我的卡位是 `[2, 5]`，我要拉起 2 个进程并告诉它们各自的 `RANK`）。

> **术语澄清**：一些 older 教材和大纲里把这段逻辑称作 `ActivePythonLauncher` 类。在当前 HEAD（`dc046b15d`）中该类名已不存在——`grep -r ActivePythonLauncher` 无结果，本仓库可查的历史（`git log -S`）也无记录；现在的实现就是 [launch.py:L135](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/launcher/launch.py#L135) 起的模块级函数 `main()`。学源码要以当前代码为准，本讲按现状讲解。

#### 4.3.2 核心流程

```text
main()
 1. 解码 --world_info（base64 → JSON → dict{主机: [卡位...]})
 2. 本机 = 节点列表中第 node_rank 台；本机卡位 = world_info[本机]
 3. 生成 global_rank_mapping：按 4.2.2 的公式给每台机每个卡位分配全局 rank
 4. 设置可见设备环境变量（如 CUDA_VISIBLE_DEVICES=本机卡位）
 5. 写入公共环境变量：
      MASTER_ADDR / MASTER_PORT / WORLD_SIZE
      CROSS_RANK(=node_rank) / CROSS_SIZE(=节点数) / LOCAL_SIZE(=本机进程数)
 6. 对本机每个卡位 i：
      RANK        = global_rank_mapping[本机][i]
      LOCAL_RANK  = RANK % 本机进程数
      cmd = [python, -u, (-m)?, 训练脚本, --local_rank=LOCAL_RANK, *用户参数]
      subprocess.Popen(cmd, env=当前环境)
      logger.info("process {pid} spawned with command: {cmd}")   ← 实践要抓的第二条日志
 7. 注册 SIGINT/SIGTERM 处理器；每秒轮询子进程，
    任一进程退出码非零 → 终止整棵进程树并以其退出码退出
```

`local_rank` 的换算（[launch.py:L212-L213](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/launcher/launch.py#L212-L213)）：

\[ \text{local\_rank} = \text{global\_rank} \bmod |\text{本机卡位数}| \]

之所以能整除成立，是因为全局编号是按"逐台机器连续分配"的——同一台机器上的 rank 一定是连续区间。

#### 4.3.3 源码精读

**world_info 解码与 rank 映射**——[launch.py:L147-L158](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/launcher/launch.py#L147-L158)：`--world_info` 缺失直接抛错；base64 解码 + `json.loads` 还原资源表；`--node_rank` 选中"我是哪台机"。[launch.py:L160-L170](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/launcher/launch.py#L160-L170) 按 4.2.2 公式生成 `global_rank_mapping` 并打印——日志里 `global_rank_mapping=...` 与 `dist_world_size=...` 两行就是这张表的直接呈现。

**可见设备与公共环境变量**——[launch.py:L172-L174](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/launcher/launch.py#L172-L174) 调加速器抽象层的 `set_visible_devices_envs`，把本机卡位写进 `CUDA_VISIBLE_DEVICES`（或对应后端的等价变量）——**这就是"每个训练进程只看得见自己那张卡"的实现点**。[launch.py:L176-L182](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/launcher/launch.py#L176-L182) 写入组网三件套 `MASTER_ADDR/MASTER_PORT/WORLD_SIZE`（默认端口 29500 来自 [deepspeed/constants.py:L12](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/constants.py#L12)），以及三个跨节点维度变量 `CROSS_RANK/CROSS_SIZE/LOCAL_SIZE`（变量名定义在 [deepspeed/constants.py:L23-L24](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/constants.py#L23-L24)）。

**每个进程的孵化循环**——[launch.py:L210-L236](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/launcher/launch.py#L210-L236)：先算 `RANK`/`LOCAL_RANK`（[L212-L215](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/launcher/launch.py#L212-L215)）；再拼命令（[L218-L236](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/launcher/launch.py#L218-L236)）：

- `[sys.executable, "-u"]`：用**当前同一个 python 解释器**、关闭 stdout 缓冲（日志实时可见）；
- `--module` 时插 `-m`（把训练脚本当模块跑）；`--no_python` 时干脆不前缀 python（脚本自身可执行）；
- [L234-L235](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/launcher/launch.py#L234-L235) 默认把 `--local_rank=N` 追加为脚本的**命令行参数**（可用 `--no_local_rank` 关掉）——所以老式训练脚本里 `parser.add_argument("--local_rank", ...)` 的出处就在这里；
- 最后拼上 `training_script_args`（runner 传来的用户参数）。

[launch.py:L241-L246](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/launcher/launch.py#L241-L246)：`Popen` 启动（`--enable_each_rank_log` 时把每个 rank 的 stdout/stderr 重定向到独立日志文件），并打印 `process {pid} spawned with command: {cmd}`。

**进程监管**——[launch.py:L248-L289](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/launcher/launch.py#L248-L289)：注册 SIGINT/SIGTERM 处理器（Ctrl+C 时用 `psutil` 终止**整棵**子进程树——训练脚本自己 fork 的 dataloader worker 也会被收掉，[L121-L132](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/launcher/launch.py#L121-L132)）；主循环每秒轮询，**任一** rank 非零退出就立即触发全员终止（[L280-L282](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/launcher/launch.py#L280-L282)）——这就是分布式训练"一个进程崩、整组陪葬"的机制来源。

#### 4.3.4 代码实践

1. **实践目标**：亲眼看到 launch.py 孵化出的**真实进程命令行**和环境变量，并与 `torchrun` 等价启动对比。
2. **操作步骤**：
   ```bash
   # ① 最小"训练脚本"：启动器并不关心脚本内容，先只打印环境（示例代码）
   cat > mini_env.py <<'EOF'
   import os, json
   keys = ["RANK", "LOCAL_RANK", "WORLD_SIZE", "MASTER_ADDR", "MASTER_PORT",
           "CROSS_RANK", "CROSS_SIZE", "LOCAL_SIZE", "CUDA_VISIBLE_DEVICES"]
   print(json.dumps({k: os.environ.get(k, "<unset>") for k in keys}, indent=2))
   print("argv =", os.sys.argv)
   EOF

   # ② 用 deepspeed 启动（GPU 机器）
   deepspeed --num_gpus 1 mini_env.py 2>&1 | tee ds_launch.log
   # CPU 机器的等价写法（加速器抽象回退到 CPU，卡数按 LOCAL_SIZE/NUMA 计）：
   #   LOCAL_SIZE=1 DS_ACCELERATOR=cpu deepspeed --num_gpus 1 mini_env.py

   # ③ 抓两条关键日志
   grep "cmd = " ds_launch.log            # runner 拼的第一级命令
   grep "spawned with command" ds_launch.log   # launch 拼的第二级命令

   # ④ torchrun 等价启动对比
   torchrun --nproc_per_node=1 mini_env.py
   ```
   说明：`DS_ACCELERATOR` 是加速器抽象层的环境变量覆盖入口（[real_accelerator.py:L59-L60](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/accelerator/real_accelerator.py#L59-L60)），CPU 后端的 `device_count()` 优先读 `LOCAL_SIZE` 环境变量（[cpu_accelerator.py:L210-L213](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/accelerator/cpu_accelerator.py#L210-L213)），所以显式设 `LOCAL_SIZE=1` 最稳。
3. **需要观察的现象**：
   - 第一级 `cmd = ` 形如 `python -u -m deepspeed.launcher.launch --world_info=<base64> --master_addr=127.0.0.1 --master_port=29500 mini_env.py`；
   - 第二级 `spawned with command` 形如 `['python', '-u', 'mini_env.py', '--local_rank=0']`；
   - mini_env 打印的 JSON 里 `RANK=0`、`LOCAL_RANK=0`、`WORLD_SIZE=1`、`MASTER_ADDR=127.0.0.1`、`MASTER_PORT=29500`，`argv` 末尾带 `--local_rank=0`。
4. **预期结果**：两级命令完全对上 4.2.3/4.3.3 的源码逻辑；torchrun 同样注入 `RANK/LOCAL_RANK/WORLD_SIZE/MASTER_*`，但**不会**往 `argv` 里塞 `--local_rank=`（新版 torchrun 只用环境变量传）——这正是 deepspeed 启动器兼容老式脚本的差异点。CPU 机器上 NUMA 探测行为依赖机器配置，若 `LOCAL_SIZE=1` 仍报 `no GPU resources available`，请检查 `DS_ACCELERATOR=cpu` 是否生效（`ds_report` 第一行能看出当前加速器）——此情形标注为**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：单机 4 卡运行 `deepspeed train.py`（不带 `--num_gpus`）时，4 个进程的 `RANK`/`LOCAL_RANK` 各是多少？
**答案**：无 hostfile 时资源表为 `{localhost: 4}`，world_size=4。按 [launch.py:L212-L215](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/launcher/launch.py#L212-L215)：RANK 分别为 0/1/2/3；LOCAL_RANK = RANK % 4，同为 0/1/2/3。单机场景两者数值相同，多机时才会分叉。

**练习 2**：为什么 launch.py 要把 `--local_rank` 作为命令行参数传，而不是只用环境变量？
**答案**：历史包袱 + 兼容性。早期 `torch.distributed.launch` 约定脚本从命令行读 `--local_rank`，大量存量训练脚本据此写了解析；DeepSpeed 保留这一行为保证这些脚本零修改可用，同时**也**设了 `LOCAL_RANK` 环境变量，新脚本推荐读环境变量；确实不想要这个参数时可加 `--no_local_rank`（[launch.py:L234-L235](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/launcher/launch.py#L234-L235)）。

**练习 3**：一个 rank 挂了，其余 rank 会怎样？依据是哪段代码？
**答案**：全部被终止。监管循环里 [launch.py:L280-L282](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/launcher/launch.py#L280-L282)：发现任一子进程 `returncode != 0` 就记录该退出码并调用 `sigkill_handler`，后者对每个子进程执行 `terminate_process_tree`（psutil 递归杀树），最终以失败进程的退出码 `sys.exit`。分布式训练中部分 rank 存活会导致其余 rank 卡在集合通信上，宁可整体退出。

### 4.4 ds_report 与 env_report.py：环境探测报告

#### 4.4.1 概念说明

训练起不来，八成是这两类问题之一：**算子没编译**（或当前环境编不出来）、**工具链版本不匹配**。`ds_report`（实现于 `env_report.py`）就是给这两类问题做的体检，输出两大块：

- **op report（算子报告）**：逐个列出 `ALL_OPS` 注册表（u1-l3 讲过的反射发现机制）里的算子，标注两列状态：
  - `instaled` 列（代码里叫 installed）：安装 DeepSpeed 时是否**预编译**了它——读的是安装期写下的 `installed_ops` 指纹（呼应 u1-l3 的"版本指纹文件"）；
  - `compatible` 列：**当前环境是否具备 JIT 编译它的条件**——运行时调用 `probe_is_compatible(builder)` 现场探测。
  - 关键认知：**`[NO] installed` 不是错误**——未预编译的算子会在第一次用到时 JIT 编译；真正要担心的是 `compatible` 也是 `[NO]`，那意味着需要时也编不出来。
- **debug report（环境报告）**：torch/deepspeed 的版本与安装路径、CUDA（或 NPU）版本链、nvcc 版本、`/dev/shm` 共享内存大小等"对版本表"要用的信息。

#### 4.4.2 核心流程

```text
ds_report → cli_main()
  ├─ op_report()
  │    ├─ 检查 ninja（JIT 的硬前提）
  │    └─ for 每个算子 in ALL_OPS:
  │         installed  = installed_ops[name] 且 当时的加速器 == 当前加速器
  │         compatible = probe_is_compatible(builder)
  └─ debug_report()
       ├─ torch 版本/路径、deepspeed 版本/git hash/分支
       ├─ [cuda 设备] torch.cuda 版本、hip 版本、nvcc 版本、wheel 编译时的 torch/cuda 版本
       ├─ [npu 设备] torch_npu 版本、CANN 版本
       └─ /dev/shm 大小检查（<512MB 告警 + NCCL 文档指引）
```

#### 4.4.3 源码精读

**数据来源**——[env_report.py:L11-L14](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/env_report.py#L11-L14)：`ALL_OPS` 来自运行期注册表，`installed_ops/torch_info/accelerator_name` 来自 `git_version_info`（u1-l3 讲过：安装时生成、记录"当时编译了什么、用什么 torch 编的"）。**版本漂移检测**就藏在 [env_report.py:L56-L57](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/env_report.py#L56-L57)：算子即使预编译过，若安装时的加速器与当前运行的加速器不一致（比如换到 CPU 机器上跑），也只显示 `[NO]`——预编译产物不可用，得走 JIT。

**op_report**——[env_report.py:L31-L60](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/env_report.py#L31-L60)：先打印一句最重要的提示（[L40-L42](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/env_report.py#L40-L42)）"未安装的算子会在运行时按需 JIT"；[L45-L47](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/env_report.py#L45-L47) 检查 ninja 是否可用（JIT 的编译驱动，不可用直接 `[FAIL]`）；[L53-L59](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/env_report.py#L53-L59) 遍历 `ALL_OPS` 逐行输出两列状态。终端上绿色 `[OKAY]`/黄色 `[NO]` 来自文件头部定义的 ANSI 颜色常量（[L16-L27](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/env_report.py#L16-L27)）。

**nvcc 版本探测**——[env_report.py:L63-L75](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/env_report.py#L63-L75)：从 `torch.utils.cpp_extension.CUDA_HOME` 找到 nvcc，执行 `nvcc -V` 并解析出版本号；找不到 CUDA_HOME 时返回醒目的红色 `[FAIL]` 提示。

**共享内存检查**——[env_report.py:L104-L121](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/env_report.py#L104-L121)：用 `os.statvfs('/dev/shm')` 算出共享内存大小（[L124-L130](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/env_report.py#L124-L130) 的 `human_readable_size` 做人类可读化），小于 512MB 时追加两条告警——Docker 用户加 `--shm-size='1gb'`，并附 NCCL 官方文档链接（NCCL 集合通信靠 `/dev/shm` 共享缓冲，这是容器里多卡训练的经典坑）。

**debug_report**——[env_report.py:L133-L164](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/env_report.py#L133-L164)：核心是**版本链对照**——[L136-L138](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/env_report.py#L136-L138) 打印当前 torch/deepspeed 的版本、路径、git hash 与分支；CUDA 设备时（[L139-L145](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/env_report.py#L139-L145)）额外打印 `torch cuda version`（torch 自带的运行时）、`nvcc version`（系统编译器）和 `deepspeed wheel compiled w.`（**安装 deepspeed 时**用的 torch/cuda）——这三行两两不一致，就是"算子加载失败/段错误"类问题的第一嫌疑。NPU 分支（[L146-L150](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/env_report.py#L146-L150)）则对照 torch_npu 与 CANN 版本。

**入口与开关**——[env_report.py:L167-L185](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/env_report.py#L167-L185)：`cli_main` 支持两个 flag——`--hide_operator_status` 跳过算子报告、`--hide_errors_and_warnings` 抑制告警；`main()` 也能被库代码直接调用（比如 autotuning 流程内部复用）。

#### 4.4.4 代码实践

1. **实践目标**：产出一份环境体检报告存档，并能逐栏解释。
2. **操作步骤**：
   ```bash
   ds_report | tee ds_report.txt
   ds_report --hide_operator_status          # 只看环境信息，验证 flag 作用
   python -c "from deepspeed.env_report import main; main()"   # 库内直调等价性
   ```
3. **需要观察的现象**：
   - 第一块表格顶部有 ninja 状态行；表格里每个算子一行，两列 `[YES]/[NO]` 与 `[OKAY]/[NO]`；
   - 第二块依次是 torch install path / torch version / deepspeed info /（cuda 机器）torch cuda version / nvcc version / deepspeed wheel compiled w. / shared memory size。
4. **预期结果**：纯 pip 安装（未设 `DS_BUILD_OPS=1`）时几乎所有算子 installed 列是 `[NO]` 而 compatible 列是 `[OKAY]`——这是**健康状态**（按需 JIT），别误读成故障；对照你 u1-l3 实践时的安装方式验证一致性。`wheel compiled w.` 的 torch 版本与 `torch version` 不同属正常（只要 compatible 为 OKAY），但若你遇到算子加载报错，这两行就是首先要核对的地方。各栏具体数值依赖本机环境，属**待本地验证**内容。

#### 4.4.5 小练习与答案

**练习 1**：报告里 `async_io` 算子显示 `[NO] installed`、`[OKAY] compatible`，需要做什么吗？
**答案**：不需要。`[NO]` 只是说安装时没预编译；`[OKAY] compatible` 表明当前环境具备编译条件，第一次真正用到（例如启用 ZeRO-Infinity 的 NVMe offload）时会自动 JIT 编译并缓存（u1-l3 讲过的运行期 JIT 路径）。想消除首次编译等待，可以 `DS_BUILD_AIO=1 pip install .` 预编译。

**练习 2**：`ds_report` 如何判断某个算子"安装过"？
**答案**：读安装期生成的 `installed_ops` 字典（`git_version_info`），且要求 `accelerator_name`（安装时记录的加速器）等于当前 `get_accelerator()._name`——两个条件同时成立才显示 `[YES]`（[env_report.py:L56-L57](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/env_report.py#L56-L57)）。所以预编译产物跨加速器类型不可用。

**练习 3**：Docker 里跑多卡 NCCL 训练偶发崩溃，`ds_report` 哪一行最值得看？
**答案**：`shared memory (/dev/shm) size` 一行。若小于 512MB，[env_report.py:L113-L120](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/env_report.py#L113-L120) 会给出黄色告警，提示 Docker 启动加 `--shm-size='1gb'` 并附 NCCL 排障文档链接——NCCL 的进程间共享缓冲依赖 `/dev/shm`，容器默认 64MB 往往不够。

## 5. 综合实践

把三个模块串成一次完整的"启动链路追踪"。目标：留下一份**两级命令行 + 环境变量快照 + torchrun 对照**的存档，作为你以后排查启动问题的基线。

**任务**：追踪 `deepspeed --num_gpus 1` 从回车到进程诞生的完整链路。

1. **体检并存档**：`ds_report | tee base_ds_report.txt`，标记出 installed/compatible 两列的含义（4.4）。
2. **准备探测脚本**（示例代码）：
   ```python
   # mini_env.py：打印启动器注入的分布式环境与 argv
   import os, json, sys
   keys = ["RANK", "LOCAL_RANK", "WORLD_SIZE", "MASTER_ADDR", "MASTER_PORT",
           "CROSS_RANK", "CROSS_SIZE", "LOCAL_SIZE", "CUDA_VISIBLE_DEVICES"]
   print(json.dumps({k: os.environ.get(k, "<unset>") for k in keys}, indent=2))
   print("argv =", sys.argv)
   ```
3. **deepspeed 启动并采集两级日志**：
   ```bash
   deepspeed --num_gpus 1 mini_env.py 2>&1 | tee trace.log
   grep -E "cmd = |WORLD INFO DICT|global_rank_mapping|spawned with command" trace.log
   ```
   （CPU 机器用 `LOCAL_SIZE=1 DS_ACCELERATOR=cpu` 前缀，见 4.3.4。）
4. **torchrun 对照**：`torchrun --nproc_per_node=1 mini_env.py 2>&1 | tee torchrun.log`，对比两者的环境变量集合与 `argv` 差异（重点：`--local_rank=` 参数是否出现在 argv、`CROSS_*`/`LOCAL_SIZE` 是否存在）。
5. **写一页结论**，包含：两级命令行原文（对应 [runner.py:L618](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/launcher/runner.py#L618) 与 [launch.py:L245](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/launcher/launch.py#L245) 两处日志点）、world_info 解码后的内容（可自行 `base64 -d` 验证与 `WORLD INFO DICT` 日志一致）、以及一张 deepspeed vs torchrun 的差异表。

验收标准：不看讲义能回答——"runner 和 launch 各自拼了哪条命令？`RANK` 是谁算出来的？`--local_rank` 是谁加的？"

## 6. 本讲小结

- `deepspeed` 命令本体是 [bin/deepspeed](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/bin/deepspeed#L1-L6) 的 6 行薄壳，真正逻辑在库里；`ds_report` 同理，对应 `env_report.py`。
- 启动分两级：**runner（前端）** 解析 `--hostfile/--include/--exclude/--num_nodes/--num_gpus` 得到资源表并 base64 编码；**launch（节点级）** 解码资源表、按公式分配全局 rank、孵化本节点全部训练进程。
- rank 分配公式：全局 rank 按主机顺序连续累加，\( \text{local\_rank} = \text{global\_rank} \bmod \text{本机卡数} \)；`WORLD_SIZE` 等于所有主机卡位数之和。
- launch 给每个进程注入 `RANK/LOCAL_RANK/WORLD_SIZE/MASTER_ADDR/MASTER_PORT/CROSS_*/LOCAL_SIZE`，并默认把 `--local_rank=N` 追加进脚本 argv（兼容老式脚本，可用 `--no_local_rank` 关闭）。
- 任一 rank 非零退出会触发整棵进程树终止并透传退出码——"一崩全崩"是刻意的分布式语义。
- `ds_report` 的算子报告里 `installed`（安装期指纹）与 `compatible`（当前 JIT 条件）是两件独立的事：`[NO] installed + [OKAY] compatible` 是健康状态；debug report 里的三行版本（torch cuda / nvcc / wheel compiled w.）与 `/dev/shm` 大小是排障第一现场。

## 7. 下一步学习建议

到这里，单元 1（认识项目）结束。你已能安装、启动、诊断，但 `mini_env.py` 还不是训练——下一单元进入**核心用法**：

1. **u2-l1（initialize() 与三步接入法）**：把 `mini_env.py` 升级成真正的训练脚本——`deepspeed.initialize(model, optimizer, config)` 返回引擎、`engine.backward()/step()` 驱动训练。你会发现 `--local_rank` 参数在脚本里被 `deepspeed.initialize` 内部消费。
2. **u2-l2（配置系统）**：启动命令里只解决了"在哪跑"，"怎么跑"（batch size、ZeRO 阶段、精度）由 JSON 配置决定，届时结合本讲的启动链路一起看日志。
3. 想先深挖**多机启动**（pdsh 命令拼装、`.deepspeed_env`、SSH 检查细节）的读者可跳读 u3-l4，但建议按主线先完成单元 2。
4. 本讲两次出现的 `get_accelerator()` 是 u3-l1 加速器抽象层的入口，届时会系统讲解 `DS_ACCELERATOR` 与十余种硬件后端的探测加载。
