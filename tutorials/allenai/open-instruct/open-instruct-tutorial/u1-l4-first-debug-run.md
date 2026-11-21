# 跑通第一个训练：debug 冒烟脚本与镜像启动

## 1. 本讲目标

学完本讲，你应该能够：

1. 读懂 `scripts/train/build_image_and_launch.sh` 的完整流程：从校验 git 状态、构建 Docker 镜像、上传 Beaker，到把镜像名交给下游脚本。
2. 理解 `scripts/train/debug/` 下的冒烟（smoke test）脚本与正式训练脚本的关系，并能改小参数在本地或 Beaker 上跑通一次 SFT。
3. 掌握仓库的 GPU 测试约定：`scripts/test/run_gpu_pytest.sh` 如何根据镜像名把测试路由到 CUDA 12 / CUDA 13 集群。
4. 理解本轮（#2298）新增的 `--moe` 镜像变体：为什么它必须搭配 `--cuda-version 13`、它为 MoE v2（`OLMoDDPModel`，如 Olmo 3.5 Small）模型补上了什么、以及在默认镜像上启动 MoE v2 模型会发生什么。

## 2. 前置知识

- **Beaker**：Allen AI 内部的实验平台。训练任务以「实验（experiment）—任务（task）」的形式提交到 GPU 集群（如 `ai2/jupiter`、`ai2/holmes`）上运行，镜像、数据集、结果工件都由 Beaker 管理。
- **mason.py**：仓库根目录的一个封装脚本，把一条普通命令（如 `accelerate launch ...`）包装成 Beaker 任务规格并提交。它在 `--` 分隔符前接收平台参数（集群、GPU 数、镜像），`--` 之后是真正要执行的训练命令。详细精读见 u9-l1。
- **Docker 镜像**：训练代码与依赖的打包单位。本仓库用多阶段 Dockerfile 生成 `cuda12` / `cuda13` / `cuda13-moe` 三种训练镜像（见 u1-l2），镜像构建好之后上传到 Beaker，任务直接引用。
- **accelerate**：Hugging Face 的启动器，负责按 `--num_processes` 拉起分布式训练进程。单卡冒烟测试就是 `accelerate launch --num_processes 1`。
- **冒烟测试（smoke test）**：用小模型、小数据、极少步数快速验证「整条链路能跑通」的测试，不追求训练效果，只追求尽快暴露环境与流程问题。
- **CUDA 12 / CUDA 13**：两代 NVIDIA 运行时。本仓库依赖组互斥（`pyproject.toml` 的 `cuda12` / `cuda13` 组），CUDA 13 镜像只能跑在驱动兼容的集群（如 `ai2/holmes`）上。
- **MoE v2 / `OLMoDDPModel`**：OLMo-core 的第二代 MoE（混合专家）模型实现（Olmo 3.5 Small 即 A4B MoE）。它的 token permutation（专家路由时的 token 重排）依赖 transformer-engine 提供的算子，这是本讲 `--moe` 变体的核心动机。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `scripts/train/build_image_and_launch.sh` | 统一启动入口：构建镜像 → 上传 Beaker → 调用下游脚本 |
| `scripts/train/debug/finetune.sh` | SFT 冒烟脚本（HF 路线，0.6B 模型、单卡） |
| `scripts/train/debug/sft_integration_test.sh` | SFT 集成冒烟脚本，与上者的差异见 4.2 |
| `scripts/test/run_gpu_pytest.sh` | GPU pytest 启动脚本，按镜像名路由集群 |
| `Dockerfile` | 三种镜像（cuda12 / cuda13 / cuda13-moe）的构建定义 |
| `CLAUDE.md` | 仓库贡献约定（是指向 `AGENTS.md` 的符号链接），记录启动简写与各家族冒烟脚本清单 |
| `open_instruct/olmo_core_utils.py` | OLMo-core 工具函数，含 `require_transformer_engine` 启动检查 |
| `open_instruct/olmo_core_finetune.py` | OLMo-core SFT 入口，展示运行期检查的调用位置 |

## 4. 核心概念与源码讲解

### 4.1 镜像构建脚本：build_image_and_launch.sh

#### 4.1.1 概念说明

`build_image_and_launch.sh` 是仓库统一的「构建 + 提交」入口。它的设计动机是：Beaker 任务运行的是**镜像里的代码**，而不是你本地 checkout 的代码，所以每次启动前必须保证「当前 commit ↔ 镜像」一一对应。脚本因此做了三件事：

1. 校验当前目录是干净的 git 仓库（无未提交改动）。
2. 以当前 commit 的短哈希为标识构建 Docker 镜像并上传 Beaker；如果 Beaker 上同名镜像的描述里已含该哈希，则跳过构建直接复用。
3. 在本地 `uv sync` 装好依赖后，把 `用户名/镜像名` 作为第一个参数传给下游脚本（通常是某个 debug 脚本或 `run_gpu_pytest.sh`）。

#### 4.1.2 核心流程

```text
解析前置旗标（--miles / --cuda-version / --moe）
        │
        ▼
校验：必须在 git 仓库内，且 git status 干净
        │
        ▼
拼镜像名 open-instruct-integration-test-<分支>-cuda<版本><变体>
        │
        ▼
Beaker 上已有该 commit 的镜像？ ──是──> 跳过构建
        │否
        ▼
docker buildx build（传 GIT_COMMIT / GIT_BRANCH / CUDA_VERSION / IMAGE_VARIANT）
        │
        ▼
beaker image create（描述里写入 git commit 与变体信息）
        │
        ▼
本地 uv sync 装依赖
        │
        ▼
bash <下游脚本> <用户名/镜像名> <其余参数>
```

#### 4.1.3 源码精读

**① `--miles` 分支先行分流**（[scripts/train/build_image_and_launch.sh:L4-L11](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/scripts/train/build_image_and_launch.sh#L4-L11)）：MILES 走自己的 `scripts/miles/build_and_launch.sh`，且不允许与 `--cuda-version` 组合（MILES 用独立钉死的运行时）。

**② `--cuda-version` 解析与取值校验**（[scripts/train/build_image_and_launch.sh:L13-L34](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/scripts/train/build_image_and_launch.sh#L13-L34)）：支持 `--cuda-version 13` 与 `--cuda-version=13` 两种写法，取值只能是 12 或 13。

**③ `--moe` 分支（本轮 #2298 新增）**（[scripts/train/build_image_and_launch.sh:L36-L45](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/scripts/train/build_image_and_launch.sh#L36-L45)）：注释写明动机——`--moe` 表示「CUDA 13 镜像 + transformer-engine」，MoE v2（`OLMoDDPModel`）模型需要它。若 `--moe` 搭配的不是 CUDA 13，直接报错退出：

```bash
if [[ "${1:-}" == "--moe" ]]; then
  if [[ "$cuda_version" != "13" ]]; then
    echo "Error: --moe needs --cuda-version 13 (only the CUDA 13 image has a transformer-engine variant)." >&2
    exit 1
  fi
  image_variant="-moe"
  shift
fi
```

**④ 用法提示**（[scripts/train/build_image_and_launch.sh:L47-L51](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/scripts/train/build_image_and_launch.sh#L47-L51)）：Usage 行已同步为 `[--cuda-version 12|13 [--moe]] SCRIPT`。

**⑤ git 干净度检查与镜像命名**（[scripts/train/build_image_and_launch.sh:L53-L73](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/scripts/train/build_image_and_launch.sh#L53-L73)）：`git status --porcelain` 非空即退出——这保证镜像里 `GIT_COMMIT` 对应的代码确实等于你本地的代码；分支名会被清理成 Beaker 合法字符，镜像名末尾拼上 `cuda${cuda_version}${image_variant}`，所以 `--moe` 镜像名会追加 `-moe` 后缀（如 `...-cuda13-moe`）。

**⑥ 构建与上传**（[scripts/train/build_image_and_launch.sh:L75-L112](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/scripts/train/build_image_and_launch.sh#L75-L112)）：先用 ghcr 的 buildcache 尝试「带缓存推送」构建，失败则退回只读缓存；`--build-arg IMAGE_VARIANT="$image_variant"` 把变体传给 Dockerfile（决定基础阶段，见 4.4）；`beaker image create` 的描述写入 `Git commit: <hash>; CUDA: <版本>[; variant: moe]`，描述同时充当「该 commit 是否已构建过」的查询依据（L79 的复用判断）。

**⑦ 收尾**（[scripts/train/build_image_and_launch.sh:L114-L128](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/scripts/train/build_image_and_launch.sh#L114-L128)）：确保 uv 存在、`uv sync` 装依赖，然后 `bash "$script" "$beaker_user/$image_name" "$@"` ——第一个参数固定是完整镜像引用，这就是所有下游脚本的 `$1` 约定。

#### 4.1.4 代码实践

1. **实践目标**：不真正提交任务，验证脚本的参数校验与入口行为。
2. **操作步骤**：
   - 在仓库根目录运行 `./scripts/train/build_image_and_launch.sh`（不带任何参数），观察 Usage 输出；
   - 运行 `./scripts/train/build_image_and_launch.sh --cuda-version 12 --moe scripts/train/debug/finetune.sh`，观察报错；
   - 故意制造一个未提交改动（如 `touch tmpfile`），再运行一次合法命令，观察 git 干净度检查的报错，然后删除 `tmpfile`。
3. **需要观察的现象**：无参数时打印两行 Usage；`--moe` 搭配 CUDA 12 时打印 `Error: --moe needs --cuda-version 13 ...` 并以非零码退出；有未提交文件时打印 `Error: Uncommitted changes detected...` 与 `git status --short` 的输出。
4. **预期结果**：三种输入都被脚本自身的校验拦下，不会走到 `docker buildx build`。
5. 上述均为只读/本地校验行为，「待本地验证」的是：在你自己的机器上是否安装了 `beaker` / `docker` CLI（未安装时脚本会在更早的步骤报 command not found）。

#### 4.1.5 小练习与答案

**练习 1**：为什么脚本要求 `git status --porcelain` 为空才能继续？
**答案**：因为镜像构建时通过 `--build-arg GIT_COMMIT` 把当前 commit 写进镜像描述，而 `docker build` 上传的是工作目录的实际文件（`COPY open_instruct open_instruct` 等）。如果允许带未提交改动构建，Beaker 上这个镜像的代码就与任何 commit 都对不上，出问题时无法复现。

**练习 2**：同一分支、同一 commit 第二次运行脚本会发生什么？
**答案**：脚本用 `beaker image get` 读同名镜像的 description（L77-L79），发现里面已含当前 `git_hash`，就打印 `Beaker image already exists for commit ...` 并跳过整个 Docker 构建与上传，直接进入 uv sync 与下游脚本调用。

**练习 3**：`--cuda-version 13 --moe` 时镜像名是什么样？
**答案**：`open-instruct-integration-test-<净化后的分支名>-cuda13-moe`（L73 把 `${image_variant}` 拼在 `cuda${cuda_version}` 之后），Beaker 描述里额外带 `variant: moe`。

### 4.2 debug 冒烟脚本：finetune.sh 与 sft_integration_test.sh

#### 4.2.1 概念说明

`scripts/train/debug/` 下是一族冒烟脚本，覆盖 SFT、DPO、GRPO、OLMo-core SFT 等路线（完整清单见 [CLAUDE.md:L38-L48](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/CLAUDE.md#L38-L48)，注意 `CLAUDE.md` 是指向 `AGENTS.md` 的符号链接）。它们与正式训练脚本的关系是：

- **同一入口、不同规模**：冒烟脚本调用的训练入口（`open_instruct/finetune.py` 等）与正式训练完全一致，只是把模型换成 0.6B、数据换成一个小数据集、步数很少。
- **双模式执行**：带 `$1`（镜像名）时经 mason 提交到 Beaker；不带参数时直接在本地跑。这样同一个脚本既能本地调试又能平台验证。

#### 4.2.2 核心流程

```text
finetune.sh / sft_integration_test.sh
        │
        ├── $1 为空 ──> 本地：uv run accelerate launch ... finetune.py（小模型/小数据）
        │
        └── $1 = <用户名/镜像名>
                │
                ▼
        uv run python mason.py --cluster ai2/jupiter --gpus 1 --pure_docker_mode ... -- \
            accelerate launch ... finetune.py
        （mason 把命令包成 Beaker 任务，用刚构建的镜像运行）
```

#### 4.2.3 源码精读

**① 训练命令本体**（[scripts/train/debug/finetune.sh:L3-L24](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/scripts/train/debug/finetune.sh#L3-L24)）：`accelerate launch --num_processes 1` 拉起 `open_instruct/finetune.py`，关键参数是冒烟规模的来源——`Qwen/Qwen3-0.6B` 小模型、`max_seq_length 1024`、单数据集 `allenai/tulu-3-sft-personas-algebra 100`（mixer 只混一个子集）、`num_train_epochs 2`、`logging_steps 1`（每步打日志，方便看 loss）。

**② 本地 / Beaker 双分支**（[scripts/train/debug/finetune.sh:L26-L47](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/scripts/train/debug/finetune.sh#L26-L47)）：`$1` 非空时把它当 `BEAKER_IMAGE`，经 `mason.py` 提交：`--cluster ai2/jupiter`、`--pure_docker_mode`（直接在镜像里跑命令，不做额外包装）、`--preemptible`（可抢占、随时可能被重排的低优先级任务，冒烟够用）、`--num_nodes 1 --gpus 1`、`--artifact_ttl 1d`（结果工件只留一天）；`$1` 为空时打印 `Running locally...` 并直接 `uv run $LAUNCH_CMD`。

**③ sft_integration_test.sh 的差异**（[scripts/train/debug/sft_integration_test.sh:L3-L24](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/scripts/train/debug/sft_integration_test.sh#L3-L24) 与 [L30-L48](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/scripts/train/debug/sft_integration_test.sh#L30-L48)）：训练命令多了 `--push_to_hub false` 与 `--try_launch_beaker_eval_jobs false`（集成测试不推模型、不触发评测任务），去掉了 `--report_to wandb`（不打外部长度跟踪）；mason 参数多了 `--no_auto_dataset_cache`（跳过启动前在本机缓存数据集的步骤）。两者的对比正好展示「同一入口，按用途裁剪副作用」。

#### 4.2.4 代码实践

1. **实践目标**：跑通一次 SFT 冒烟并读到 loss 曲线。
2. **操作步骤**：
   - 本地方式（需要 GPU 与 u1-l2 装好的环境）：直接运行 `bash scripts/train/debug/finetune.sh`（不带参数即本地模式）。脚本已经用了 0.6B 模型与 1024 序列长，若想更快，把脚本复制到仓库外（如 `/tmp/finetune_small.sh`），将 `--num_train_epochs 2` 改为 `1`、`--max_seq_length 1024` 改为 `512`、`--per_device_train_batch_size 1 --gradient_accumulation_steps 4` 保持不变，再运行；
   - Beaker 方式（需要 Beaker 凭据）：`./scripts/train/build_image_and_launch.sh scripts/train/debug/finetune.sh`，脚本会自动构建镜像并把镜像名作为 `$1` 传入。
3. **需要观察的现象**：终端（或 Beaker 日志）里每步打印一次 `{'loss': ...}`；前几十步 loss 整体下降。
4. **预期结果**：训练正常结束，无 OOM、无数据加载报错；记录前 10 步与最后 10 步的 loss 值即可得到一条简易「loss 曲线」。
5. 本环境无 GPU 与 Beaker 凭据，实际数值「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：冒烟脚本与正式训练脚本的本质区别在哪里？
**答案**：入口与代码路径完全相同（都是 `open_instruct/finetune.py`），区别只在规模与副作用：小模型、小数据集、少步数、可抢占的短任务、工件短保留。这保证冒烟通过≈链路（数据加载、训练循环、日志）可用。

**练习 2**：`finetune.sh` 里 `--pure_docker_mode` 起什么作用？
**答案**：让 mason 以「纯 Docker 模式」提交任务——Beaker 直接在指定镜像里执行 `--` 之后的命令，不再额外生成启动包装；冒烟脚本要的就是「镜像里有什么就跑什么」。

**练习 3**：为什么 `sft_integration_test.sh` 要加 `--no_auto_dataset_cache` 而 `finetune.sh` 不加？
**答案**：`--no_auto_dataset_cache` 跳过 mason 在提交前于本机预缓存数据集的步骤（该步骤会用 `--cache_dataset_only` 先跑一遍训练入口）。集成测试追求环境纯净、由 CI 镜像内自行加载数据；普通冒烟保留自动缓存可以减少任务冷启动。另见 CLAUDE.md 中 macOS 上 GRPO 脚本必须加此旗标的说明（那些入口在模块级 import vllm）。

### 4.3 GPU 测试约定：run_gpu_pytest.sh 与 CLAUDE.md

#### 4.3.1 概念说明

CI 运行在 GitHub 托管的无 GPU runner 上，所以凡是触及 GPU 代码路径的改动都要**手动**跑 GPU 测试，并在 PR 正文记录 `GPU_TESTS=[实验ID]` 链接（[CLAUDE.md:L49-L50](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/CLAUDE.md#L49-L50)）。约定入口是：

```bash
./scripts/train/build_image_and_launch.sh scripts/test/run_gpu_pytest.sh
```

`run_gpu_pytest.sh` 拿到镜像名后，要解决的唯一问题是：**这个镜像该提交到哪些集群**——CUDA 13 镜像需要驱动兼容的集群（`ai2/holmes`），CUDA 12 镜像跑在 `ai2/jupiter` / `ai2/ceres` / `ai2/saturn`。

#### 4.3.2 核心流程

```text
BEAKER_IMAGE（来自 build_image_and_launch.sh 的 $1）
        │
        ▼
case 镜像名:
    *-cuda13 | *-cuda13-moe  ──> CUDA_VERSION=13, 集群 = (ai2/holmes)
    其他（含 *-cuda12）       ──> CUDA_VERSION=12, 集群 = (ai2/jupiter ai2/ceres ai2/saturn)
        │
        ▼
拼 --cluster 参数并提交 pytest 任务
```

#### 4.3.3 源码精读

**① 镜像名路由**（[scripts/test/run_gpu_pytest.sh:L14-L23](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/scripts/test/run_gpu_pytest.sh#L14-L23)）：本轮 #2298 把匹配模式从 `*-cuda13` 扩展为 `*-cuda13|*-cuda13-moe`。bash 的 `case` 模式必须**整串匹配**：`foo-cuda13-moe` 并不以 `-cuda13` 结尾，旧模式下会落进 `*` 分支被当成 CUDA 12 镜像路由到 jupiter/ceres/saturn——那些集群的驱动跑不了 CUDA 13 镜像，任务会失败。这就是模式必须扩展的原因。

**② 启动简写与脚本清单**（[CLAUDE.md:L37-L48](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/CLAUDE.md#L37-L48)）：L37 是统一启动约定 `./scripts/train/build_image_and_launch.sh [--cuda-version 12|13 [--moe]] SCRIPT`，并写明 MoE v2（`OLMoDDPModel`）模型（如 Olmo 3.5 Small）需要 `--cuda-version 13 --moe`；L38-L48 按家族列出各冒烟脚本（GRPO 三种、OLMo-core SFT 两种、DPO 三种），是选择冒烟脚本的速查表。

#### 4.3.4 代码实践

1. **实践目标**：理解镜像名 → 集群的路由规则，能预判任意变体镜像的落点。
2. **操作步骤**：阅读 [scripts/test/run_gpu_pytest.sh:L14-L23](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/scripts/test/run_gpu_pytest.sh#L14-L23)；然后在本地用 bash 验证模式匹配行为（示例代码，非项目代码）：

   ```bash
   for img in foo-main-cuda12 foo-main-cuda13 foo-main-cuda13-moe; do
     case "$img" in
       *-cuda13) echo "$img -> 旧模式: cuda13/holmes";;
       *)        echo "$img -> 旧模式: cuda12";;
     esac
   done
   ```

3. **需要观察的现象**：旧模式下 `foo-main-cuda13-moe` 被判为 cuda12。
4. **预期结果**：把模式换成 `*-cuda13|*-cuda13-moe)` 再跑，三个镜像分别落到 cuda12、cuda13、cuda13，与脚本当前行为一致。
5. 本地 bash 即可验证，无需 GPU。

#### 4.3.5 小练习与答案

**练习 1**：一个 `-cuda13-moe` 镜像的 GPU 测试会落在哪个集群？为什么？
**答案**：`ai2/holmes`。`run_gpu_pytest.sh` 的 case 分支 `*-cuda13|*-cuda13-moe` 把两种 CUDA 13 镜像都路由到 holmes；它是 CUDA 13 驱动兼容的集群，而 moe 变体恰恰是 CUDA 13 镜像加料。

**练习 2**：如果不在 `case` 里加 `*-cuda13-moe`，会发生什么？
**答案**：`case` 的模式做整串匹配，`...-cuda13-moe` 不匹配 `*-cuda13`，落入 `*` 分支被当成 CUDA 12 镜像提交到 `ai2/jupiter ai2/ceres ai2/saturn`，这些集群无法运行 CUDA 13 镜像，测试任务失败。

**练习 3**：CI 会自动跑这些 GPU 测试吗？
**答案**：不会。CI 在 GitHub 托管 runner 上运行，没有 Beaker 实验与镜像发布；GPU 测试需要手动启动，并在 PR 正文记录 `GPU_TESTS=[EXPERIMENT_ID](https://beaker.org/ex/EXPERIMENT_ID)`（CLAUDE.md 明确 CI 不解析该行，仅供审阅者）。

### 4.4 --moe 镜像变体：MoE v2 的 transformer-engine（#2298 更新）

#### 4.4.1 概念说明

MoE v2 模型（`OLMoDDPModel`，如 Olmo 3.5 Small / A4B）在专家路由时要做 **token permutation**——把 token 按所属专家重排再送入各组专家计算。OLMo-core 对这个算子的实现是调用 transformer-engine（TE）的 `permutation` 接口，且是**带守卫的 import**：TE 缺失时不是崩溃，而是把算子置为 `None`，问题被推迟到第一次 forward 才爆出来，报错信息还很难看懂。

因此 #2298 打通了一条完整的防线，三层各司其职：

1. **构建层**：Dockerfile 新增 `cuda13-moe` 基础阶段，安装 TE 2.16.1 并处理 cuBLAS 兼容问题；默认镜像（cuda12/cuda13）完全不变。
2. **启动层**：`build_image_and_launch.sh --moe` 只在 CUDA 13 下放行，把 `IMAGE_VARIANT=-moe` 传给 Docker build，镜像名追加 `-moe`。
3. **运行层**：`olmo_core_utils.require_transformer_engine()` 在 MoE v2 训练启动早期显式检查，缺 TE 时给出带修复指引的 `RuntimeError`。

为什么 TE 不直接装进默认镜像？Dockerfile 注释给了答案：TE 2.16.1 的预编译 cu13 库会调用 `cublasLtGroupedMatrixLayoutInit_internal` 符号，而 torch 自带捆绑的 cuBLAS（nvidia-cublas 13.1.0.3）并不导出它；解决办法是把 venv 里 torch 捆绑的 `libcublas`/`libcublasLt` 指向基础镜像里更新的系统副本——但这会**改变镜像里所有代码使用的 cuBLAS**，所以必须做成独立变体，默认镜像不能受影响。

#### 4.4.2 核心流程

```text
./scripts/train/build_image_and_launch.sh --cuda-version 13 --moe SCRIPT
        │  启动层：cuda_version 必须是 13，否则报错退出（L36-L45）
        ▼
IMAGE_VARIANT="-moe" ──build-arg──> Dockerfile
        │
        ▼
构建层：FROM cuda${CUDA_VERSION}${IMAGE_VARIANT} → 选中 cuda13-moe 基础阶段
        （nvidia/cuda:13.3.1-cudnn + DS_SKIP_CUDA_CHECK=1）
        装构建工具 → 装 transformer-engine 2.16.1（绑定 venv 的 torch/cuDNN）
        → 把 venv 内 cuBLAS 换成系统副本 → nm 检查符号存在
        │
        ▼
镜像名 <分支>-cuda13-moe 上传 Beaker
        │
        ▼
运行层：olmo_core_finetune.py 发现模型是 OLMoDDPModel
        → require_transformer_engine()：TE 算子为 None 则 RuntimeError（含修复指引）
```

#### 4.4.3 源码精读

**① 变体选择的基础阶段**（[Dockerfile:L1-L11](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/Dockerfile#L1-L11)）：新增 `ARG IMAGE_VARIANT=""` 与 `cuda13-moe` 阶段（基于 `nvidia/cuda:13.3.1-cudnn-devel-ubuntu22.04`，并设 `DS_SKIP_CUDA_CHECK=1`——torch 按 CUDA 13.0 构建，没有这个环境变量 DeepSpeed 会拒绝用 13.3 工具链 JIT 构建它的算子）；最终阶段 `FROM cuda${CUDA_VERSION}${IMAGE_VARIANT}` 按两个构建参数组合选基础，默认（空变体）时行为与改动前完全一致。

**② TE 安装与 cuBLAS 处理**（[Dockerfile:L88-L112](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/Dockerfile#L88-L112)）：整块逻辑包在 `if [ "$IMAGE_VARIANT" = "-moe" ]` 里，因此**只有 -moe 镜像执行**。步骤依次是：装 cmake/ninja/pybind11 等构建工具 → 以 venv 内的 torch 与 cuDNN 头文件为编译环境 `uv pip install --no-build-isolation "transformer-engine[pytorch]==2.16.1"` → 把 venv 内 torch 捆绑的 `libcublasLt`/`libcublas` 软链到 `/usr/local/cuda/lib64` 的系统副本 → 最后用 `nm -D` 确认 `cublasLtGroupedMatrixLayoutInit_internal` 符号确实存在（构建期自检，符号缺失会让 `grep -q` 失败、构建报错）。

**③ 启动层的门禁**（[scripts/train/build_image_and_launch.sh:L36-L45](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/scripts/train/build_image_and_launch.sh#L36-L45)）：即 4.1 精读③——`--moe` 搭配 CUDA 12 直接报错，因为 Dockerfile 只定义了 `cuda13-moe` 阶段，CUDA 12 下没有对应变体可构建。

**④ 运行层的守卫**（[open_instruct/olmo_core_utils.py:L575-L587](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/open_instruct/olmo_core_utils.py#L575-L587)）：`require_transformer_engine` 检查 `oc_moe_utils.moe_permute is None`（OLMo-core 在 TE 缺失时把它置为 None），抛出的 `RuntimeError` 直接告诉用户该用哪条命令：

```python
raise RuntimeError(
    "MoE v2 (OLMoDDPModel) training needs transformer-engine: OLMo-core's MoE token permutation "
    "calls transformer_engine.pytorch.permutation, which is not importable here. Only open-instruct's "
    "MoE image installs it: launch with `scripts/train/build_image_and_launch.sh --cuda-version 13 "
    "--moe SCRIPT`."
)
```

**⑤ 守卫的调用位置**（[open_instruct/olmo_core_finetune.py:L651-L658](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/open_instruct/olmo_core_finetune.py#L651-L658)）：`setup_model` 之后判断 `isinstance(model, ddp_model_lib.OLMoDDPModel)`（MoE v2 拒绝 FSDP、走 DDP 训练模块），是 MoE v2 才调用 `require_transformer_engine()`——注意时机在训练开始前、`_check_ddp_model_args` 之后，所以错误在启动早期就暴露，而不是第一次 forward 时。

**⑥ 与 SFT 配方的衔接**（[docs/algorithms/sft_running_guide.md:L24-L41](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/docs/algorithms/sft_running_guide.md#L24-L41)）：A4B 配方（`uv run python scripts/train/sft.py --recipe a4b`）正因为上述原因**钉死自己的镜像**——文档原话是 main 的镜像没有 A4B MoE 所需的 transformer-engine。配方系统的精读见 u7-l5。

#### 4.4.4 代码实践

1. **实践目标**：回答两个问题——为什么 `--moe` 必须搭配 `--cuda-version 13`？在默认镜像上启动 MoE v2 模型会发生什么？
2. **操作步骤**：
   - 通读 [scripts/train/build_image_and_launch.sh:L36-L45](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/scripts/train/build_image_and_launch.sh#L36-L45) 与 [Dockerfile:L1-L11](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/Dockerfile#L1-L11)，确认变体阶段的命名规则；
   - 通读 [open_instruct/olmo_core_utils.py:L575-L587](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/open_instruct/olmo_core_utils.py#L575-L587) 与 [open_instruct/olmo_core_finetune.py:L651-L658](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/open_instruct/olmo_core_finetune.py#L651-L658)，跟踪「模型是 OLMoDDPModel → 调用守卫」的条件链；
   - 对照 [CLAUDE.md:L37](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/CLAUDE.md#L37) 的启动约定，写出在 Beaker 上为 Olmo 3.5 Small 跑 OLMo-core SFT 冒烟的完整命令骨架。
3. **需要观察的现象**：纯源码阅读型实践；若在本地对一个 MoE v2 checkpoint 运行 `olmo_core_finetune.py`（无 TE 环境），会在启动早期看到带 `launch with ... --moe SCRIPT` 指引的 RuntimeError。
4. **预期结果（参考答案）**：
   - **为什么必须 CUDA 13**：Dockerfile 只定义了 `cuda13-moe` 这一个 TE 变体阶段，最终阶段 `FROM cuda${CUDA_VERSION}${IMAGE_VARIANT}` 在 CUDA 12 + `-moe` 时指向不存在的 `cuda12-moe`，构建必然失败；启动脚本因此在 L39-L41 提前拦截。TE 2.16.1 的预编译库也只针对 cu13。
   - **默认镜像上启动 MoE v2 会怎样**：OLMo-core 把 TE 的 permutation 算子守卫式 import、缺失时置 None；`require_transformer_engine()` 在 `setup_model` 判定模型是 `OLMoDDPModel` 后立即抛 RuntimeError，报错文本直接给出正确命令 `--cuda-version 13 --moe`。没有这个守卫的话，失败会推迟到第一次 forward，报错难懂得多。
   - **命令骨架**：`./scripts/train/build_image_and_launch.sh --cuda-version 13 --moe scripts/train/debug/<你的脚本>.sh`（集群需选 CUDA 13 兼容的 `ai2/holmes`）。
5. 实际报错文本「待本地验证」（本环境无 GPU 与 MoE checkpoint）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 TE 不装进默认镜像，而非要单开一个变体？
**答案**：TE 2.16.1 需要的 `cublasLtGroupedMatrixLayoutInit_internal` 符号在 torch 捆绑的 cuBLAS 里没有，修法是把 venv 内的 cuBLAS 换成系统副本——这改变镜像里**所有**代码的 cuBLAS，可能影响既有训练的可复现性；且 TE 绑定按 CUDA 13 构建。所以只让显式选择 `--moe` 的用户承担这个变化，默认镜像保持原样。

**练习 2**：`cuda13-moe` 基础阶段里 `DS_SKIP_CUDA_CHECK=1` 解决什么问题？
**答案**：torch 是按 CUDA 13.0 构建的，而该阶段基座是 CUDA 13.3.1；不设此变量时 DeepSpeed 检测到版本不一致会拒绝 JIT 构建自己的算子（如 CPU Adam）。设置后允许用 13.3 工具链继续编译。

**练习 3**：`require_transformer_engine` 为什么放在 `olmo_core_finetune.py` 的 `is_ddp_model` 分支里，而不是模块 import 时？
**答案**：只有 MoE v2（`OLMoDDPModel`）模型才需要 TE；dense 模型走 FSDP 路线完全用不到。放在分支里让非 MoE 训练不受影响，同时在 MoE 训练的启动早期（模型构建完成、训练循环开始前）就把「缺 TE」变成一条可操作的报错。

## 5. 综合实践

把本讲四条线索串成一次完整的「从镜像到 loss 曲线」演练：

1. **跑通冒烟**：按 4.2.4 在本地（或 Beaker）跑通 `scripts/train/debug/finetune.sh`，记录前 10 步与最后 10 步的 loss，画出简易曲线；再用 4.1.4 的方法观察启动脚本的三道校验（Usage / `--moe` 与 CUDA 12 组合 / git 不干净）。
2. **预判路由**：假设你的分支叫 `feat-x`，分别写出用 `--cuda-version 12`、`--cuda-version 13`、`--cuda-version 13 --moe` 三种方式构建出的镜像名，并对照 4.3 判断 `scripts/test/run_gpu_pytest.sh` 会把每个镜像提交到哪个集群。
3. **走通 MoE 链路**：按 4.4.4 阅读 `--moe` 的三层防线（Dockerfile 变体 → 启动脚本门禁 → 运行期守卫），写一段 5-8 句的说明：为什么 Olmo 3.5 Small 的 SFT 配方（`scripts/train/sft.py --recipe a4b`）要钉死自己的镜像，而普通 dense 模型的冒烟用默认镜像即可。
4. 交付物：一份包含 loss 记录、三个镜像名与集群判断、MoE 链路说明的短笔记。Beaker 与 GPU 相关数值「待本地验证」。

## 6. 本讲小结

- `build_image_and_launch.sh` 是统一启动入口：校验 git 干净 → 按当前 commit 构建/复用 Beaker 镜像 → `uv sync` → 以镜像名为 `$1` 调用下游脚本。
- `scripts/train/debug/` 的冒烟脚本与正式训练共用同一入口，只是缩小规模并裁剪副作用；带 `$1` 走 mason/Beaker，不带则本地运行。
- GPU 测试需手动启动：`run_gpu_pytest.sh` 按镜像名整串匹配路由集群，`*-cuda13` 与 `*-cuda13-moe` 都去 `ai2/holmes`，其余按 CUDA 12 处理。
- #2298 新增 `--moe` 变体（仅限 `--cuda-version 13`）：镜像名追加 `-moe`，`IMAGE_VARIANT` 传给 Dockerfile 选中加装 transformer-engine 2.16.1 的 `cuda13-moe` 基础阶段，默认镜像不变。
- MoE v2（`OLMoDDPModel`）的 token permutation 依赖 TE；在默认镜像上启动时，`require_transformer_engine()` 会在启动早期抛出带 `--cuda-version 13 --moe` 修复指引的 RuntimeError。

## 7. 下一步学习建议

- 下一讲（u2-l1）进入 `open_instruct/finetune.py` 本体：你在冒烟脚本里见过的每个参数（`dataset_mixer_list`、`max_seq_length`、`chat_template_name`……）都会在参数定义中逐一展开。
- 想深入 mason 的任务规格组装（`--pure_docker_mode`、`--preemptible`、`--min_runtime` 等），提前阅读 `mason.py`，对应 u9-l1。
- 想了解 MoE v2 SFT 的完整配方（`a4b` 固定镜像、断点续跑、显存工程设置），阅读 `scripts/train/sft.py` 与 `docs/algorithms/sft_running_guide.md`，对应 u7-l5。
- 镜像构建细节（cuda12/cuda13/cuda13-moe 三变体、依赖组切换）已在 u1-l2 讲过，可回看对照。
