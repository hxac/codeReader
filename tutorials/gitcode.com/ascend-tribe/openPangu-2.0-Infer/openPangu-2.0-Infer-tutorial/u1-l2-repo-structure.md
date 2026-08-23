# 仓库目录结构与四大组件划分

> 前置讲义：[u1-l1 项目定位与全景](u1-l1-project-overview.md)。上一讲我们已经知道 openPangu-2.0-Infer 是一个「部署仓」，通过 ansible + docker 在昇腾 NPU 上以 PD 分离形态部署 openPangu-2.0 系列模型，并且认识了两类角色：**运行组件**（omni-npu / omni-proxy / omni-cache / omni-eplb）和**部署工具**。本讲我们把镜头拉近，看清这些代码在仓库里到底怎么摆放、怎么被组织、以及一条 `build.sh` 命令背后发生了什么。

## 1. 本讲目标

学完本讲，你应该能够：

1. **画出仓库顶层目录树**，说出 `components/` 与 `tools/` 两大目录的分工——前者是「跑在 NPU 上的运行组件」，后者是「帮你把它们部署起来的工具链」。
2. **解释 git submodule（子模块）机制**在本仓库中的用法：四个组件各自是独立的 git 仓库，顶层仓通过 `.gitmodules` + `build/build.sh` 把它们拉到 `components/` 下统一编译。
3. **读懂构建入口** `build/build.sh` 的参数解析（`parse_args`）与编译分发（`check_and_build`），能写出「只编译 omni-npu 一个模块」的完整命令，并准确预测它会调用哪个子目录下的脚本。
4. 理解一个重要且容易被忽视的事实：**当前 gitcode 开源仓里 `.gitmodules` 文件并不存在，四个组件是以普通目录形式平铺在本仓里的**——这会直接影响你在本地怎么使用 `build.sh`。

## 2. 前置知识

本讲只需要三个通俗概念，不需要写过程序也能跟上：

- **monorepo（单仓多项目）**：把多个可以独立开发、独立编译的项目放进同一个仓库管理。好处是「一次 clone，全家到手」，部署脚本可以按固定相对路径（如 `components/omni-npu`）找到每个组件；代价是需要一套机制把子项目「挂」进来——本仓用的是 git submodule。
- **git submodule（子模块）**：git 允许仓库 A 在自己的某个目录下记录「仓库 B 的地址 + 一个固定的 commit 号」。父仓里这个目录只存一个指针（gitlink），不存 B 的具体文件内容；执行 `git submodule update` 时 git 才会去 B 的地址把那个 commit 的内容拉下来。子模块的地址统一登记在一个叫 `.gitmodules` 的文本文件里。你可以把它理解为：**父仓是一本目录册，`.gitmodules` 是附录里的「分册来源清单」**。
- **构建入口（build entry）**：一个项目约定俗成的一个脚本（常见叫 `build.sh` 或 `Makefile`）， newcomers 只需要会跑它，不需要记住每个子项目的编译细节。本仓的约定是：**顶层一个总入口 `build/build.sh`，每个组件目录下再各有一个 `build/build.sh`**，总入口负责「拉代码 + 逐个调用子入口」。

另外一个背景知识来自上一讲：四个组件里，omni-npu 是 vLLM 的 NPU 平台插件（服务运行的地基），omni-proxy 是请求调度引擎，omni-cache 是主机内存 KV 缓存池，omni-eplb 是 MoE 专家负载均衡器。本讲关心的是它们的**物理摆放与编译方式**，而不是各自的内部原理（那是后续单元的事）。

## 3. 本讲源码地图

| 文件 / 目录 | 作用 |
|---|---|
| [README.md](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README.md) | 顶层部署说明：模型规格与配置目录对照表、镜像、ansible 拉起流程 |
| [build/build.sh](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/build/build.sh) | **顶层构建总入口**：解析参数、初始化/更新子模块、逐个调用组件的 `build/build.sh` |
| [components/omni-npu/README.md](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/README.md) | omni-npu 组件自述：vLLM 0.14.0 out-of-tree 插件的定位与安装顺序 |
| [components/omni-npu/build/build.sh](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/build/build.sh) | omni-npu 的子构建脚本（顶层 `check_and_build` 的调用目标，仅 5 行） |
| `components/omni-cache/build/build.sh`、`components/omni-eplb/build/build.sh`、`components/omni-proxy/build/build.sh` | 其余三个组件的子构建脚本，用来对比「同一约定、不同产物」 |
| `tools/ansible/92B/`、`tools/ansible/505B/` | 部署工具链示例：两种模型规格各自的 inventory 与服务模板 |

## 4. 核心概念与源码讲解

本讲的三个最小模块：**monorepo 结构**、**git submodule**、**构建入口**。

### 4.1 monorepo 结构：components/ 与 tools/ 的分工

#### 4.1.1 概念说明

第一讲我们按「职责」认识了四个组件；本讲按「物理位置」重新看一遍。整个仓库只有两个真正装代码的顶层目录：

- **`components/` —— 运行组件**：这些代码最终会**装进推理容器里、参与每一次推理请求**。顶层 README 里列出的镜像预装包 `omni-npu 0.2.0` 就来自这里的某个组件。四个组件每个都是一个完整独立项目：有自己的 README、`pyproject.toml`（或 RPM spec）、`src`/源码目录、`tests`、以及**自己的 `build/build.sh`**。
- **`tools/` —— 部署工具链**：这些代码**不会装进推理服务**，它们的任务是「把运行组件部署到一排机器上」：ansible 负责多机编排、docker 负责镜像、quant 里的 jointfix 负责离线量化权重、scripts 里是拉起服务用的 shell/python 脚本。

一句话区分：**改 `components/` 影响推理行为；改 `tools/` 影响部署方式。**

#### 4.1.2 核心流程

仓库顶层目录树（按当前 HEAD 的实际内容整理）：

```text
openPangu-2.0-Infer/
├── README.md / README_EN.md          # 部署说明（中/英，BF16 主流程）
├── README_INT8.md / README_INT8_EN.md # 部署说明（中/英，INT8/W8A8 流程）
├── LICENSE / OPEN SOURCE SOFTWARE NOTICE
├── build/
│   └── build.sh                       # 顶层构建总入口（本讲主角）
├── components/                        # ← 运行组件（四个独立子项目）
│   ├── omni-npu/     # vLLM NPU 平台插件（Python，pip 包）
│   ├── omni-cache/   # 主机内存 KV 缓存池（Python + C++ 扩展，pip 包）
│   ├── omni-eplb/    # MoE 专家负载均衡（Python + C++/cmake，pip 包）
│   └── omni-proxy/   # 请求调度引擎（C 语言 nginx 动态模块，RPM 包）
└── tools/                             # ← 部署工具链（不进入推理服务）
    ├── ansible/     # 92B/505B 两套 inventory + 服务模板
    ├── docker/      # Dockerfile.base / Dockerfile.omniinfer / 构建脚本
    ├── quant/       # jointfix：W8A8 训练后量化工具箱
    └── scripts/     # pd_run.sh / start_api_servers.py / bind_cpu.sh
```

`tools/ansible` 内部再按模型规格分为两个目录，这一点顶层 README 的对照表写得很明白：

| 模型规格 | 模型名称 | 配置文件目录 | 典型部署 |
|---------|---------|-------------|------------|
| 92B | openPangu-2.0-Flash | `tools/ansible/92B/` | 1P1D（2 机 A3） |
| 505B | openPangu-2.0-Pro | `tools/ansible/505B/` | 2P1D（8 机 A3） |

实际目录里，`tools/ansible/92B/` 含 3 份 inventory（`1P1D`、`1P1D_A2`、`3P1D`）和 4 份服务模板（bf16、w8a8、A2_w8a8、3P1D_w8a8_omni_cache）；`tools/ansible/505B/` 含 2 份 inventory（`2P1D`、`4P81D16`）和 3 份服务模板。**inventory 回答「部署到哪些机器」，模板回答「每台机器跑什么命令」**——这是第三讲的内容，这里只需记住位置。

#### 4.1.3 源码精读

顶层 README 开头就给出了仓库的使用方式：多机部署通过 `ansible-playbook` 统一拉起，脚本位于 `tools/ansible/92B` 和 `tools/ansible/505B`：

- [README.md:L49-L57](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README.md#L49-L57) —— 说明拉起 PD 分离服务的脚本在 `tools/ansible/92B` 与 `tools/ansible/505B` 路径下，并列出 1P1D 需要修改的两个文件（inventory 与服务模板）。这说明：**日常使用者接触最多的是 `tools/`，而不是 `components/`**——组件已经被打进了 docker 镜像。

- [README.md:L118-L125](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README.md#L118-L125) —— 「推理代码适配」一节：若需要修改推理代码，进入 docker 后用 `pip list | grep omni-npu` 查看组件在容器内的安装路径再改。这印证了 components/ 与 tools/ 的分工：**组件以 pip 包形式活在容器里，仓库里的 `components/` 源码是你修改后重新编译的「源头」**。

- [components/omni-npu/README.md:L1-L7](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/README.md#L1-L7) —— omni-npu 的自我定位：一个 vLLM 0.14.0 的 out-of-tree（树外）平台插件，让 vLLM 原封不动地跑在 NPU 上。注意它是一个**独立项目**：有自己的 README、安装说明（[L15-L29](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/README.md#L15-L29) 要求先装 vllm==0.14.0 与 torch_npu，再 `pip install .` 装自己）。四个组件都是这种「离开本仓也能独立编译」的项目，这正是它们能被 submodule 管理的前提。

#### 4.1.4 代码实践：亲手验证目录分工

1. **实践目标**：不靠背诵，用 git 命令自己「发现」仓库结构，并验证四个组件在当前仓里是被当作普通文件追踪的。
2. **操作步骤**（在本仓根目录执行）：
   ```bash
   # a) 列出顶层被 git 追踪的文件，观察有哪些顶层目录
   git ls-files | cut -d/ -f1 | sort | uniq -c
   # b) 统计四个组件各自的文件数量（证明它们的源码都在本仓里）
   for m in omni-npu omni-cache omni-eplb omni-proxy; do
       echo -n "$m: "; git ls-files "components/$m" | wc -l
   done
   # c) 确认每个组件都有自己的构建脚本
   ls components/*/build/build.sh
   ```
3. **需要观察的现象**：命令 a) 的输出里顶层只有 `build/`、`components/`、`tools/` 三个目录承载代码（外加若干 README/LICENSE 等文件）；命令 b) 中四个组件各返回一个不小的数字（说明源码被完整平铺追踪，而不是空目录或指针）；命令 c) 恰好列出 4 个 `build/build.sh`。
4. **预期结果**：你会看到 `components/` 是仓库的代码主体，`tools/` 次之。这个实验同时给出了 4.2 节的关键证据——**当前仓库里组件不是 submodule gitlink，而是普通文件**（若是 submodule，`git ls-files components/omni-npu` 只会显示一行目录名，而不是几百行具体文件）。上述具体输出数值「待本地验证」，但定性结论可由 git 语义直接推出。

#### 4.1.5 小练习与答案

**练习 1**：你想给 omni-proxy 的调度器加一个新功能，改动应该放在 `components/` 还是 `tools/`？改完后要不要重新制作 docker 镜像？

**答案**：放 `components/omni-proxy/`（它是运行组件）。是否重做镜像取决于安装方式：omni-proxy 以 RPM 形式安装（见 4.3.3 的对比表），改完需要重新构建 RPM 并更新到目标机器（或重做镜像层）；而 `tools/` 下的改动（例如改 ansible 模板）只需重跑 playbook，不需要动镜像。

**练习 2**：`tools/quant/jointfix` 是做量化用的，为什么它不在 `components/` 下？

**答案**：量化是**部署前的离线步骤**——jointfix 把 BF16 权重转成 W8A8 权重，产出新的权重目录后就功成身退，不参与线上推理请求的任何环节。按本仓「运行组件 / 部署工具」的二分法，它属于部署工具链，所以在 `tools/` 下。

### 4.2 git submodule：四个组件如何被组织

#### 4.2.1 概念说明

上一节我们看到 `components/` 下是四个完整独立的项目。它们和顶层仓的关系，由 `build/build.sh` 中的一段**仓库映射表**给出答案——四个组件各自对应一个独立的 gitee 仓库：

| 模块名 | 独立仓库地址（master 分支） | 挂载到 |
|---|---|---|
| omni-npu | `https://gitee.com/omniai/omni-npu.git` | `components/omni-npu` |
| omni-cache | `https://gitee.com/omniai/omni-cache.git` | `components/omni-cache` |
| omni-eplb | `https://gitee.com/omniai/omni-eplb.git` | `components/omni-eplb` |
| omni-proxy | `https://gitee.com/omniai/omni-proxy.git` | `components/omni-proxy` |

**为什么用 submodule 而不是把四个项目揉成一个仓？** 三个理由：

1. **独立演进**：omni-npu 迭代很快（跟随 vLLM 适配 NPU），omni-proxy 是 C 项目有自己的发布节奏；分开建仓后各自的提交历史、分支、CI 互不干扰。
2. **版本锁定**：父仓只记录「每个子模块停在哪个 commit」，顶层做一个稳定版本时可以精确钉住四个组件的组合，`build.sh` 还支持用 `-s 模块=commit` 把某个模块钉到指定提交（见 [build/build.sh:L234-L242](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/build/build.sh#L234-L242)）。
3. **可选编译**：不是所有部署形态都需要全部四个组件（比如不开专家负载均衡就不用 omni-eplb），submodule 模式天然支持按需拉取按需编译。

**但必须诚实地指出当前仓库的特殊形态**：在 gitcode 上的这个开源仓里，**根目录没有 `.gitmodules` 文件**，四个组件的源码是以普通文件形式直接纳入本仓版本管理的（4.1.4 的实践已经验证了这一点）。也就是说：`build/build.sh` 保留的是**上游「部署仓 + 子模块」工作流的完整逻辑**，而 gitcode 仓把子模块内容平铺固化了下来。这带来的实际影响在 4.2.4 实践中会亲手验证。

#### 4.2.2 核心流程

`build/build.sh` 管理子模块的流程（对应 `init_submodules` 函数）：

```text
对每个待编译模块 mod（来自 -m 参数，或默认全部）：
  1. 若 components/mod 已存在：
     备份 .gitmodules → git submodule deinit → git rm → 删除残留 → 还原 .gitmodules
     （即"先清场再重挂"，保证拿到干净状态）
  2. 从 .gitmodules 读出该模块的 url / path / branch
  3. git submodule add --force -b <branch> <url> <path>
  4. git submodule init
  5. git submodule update --recursive --remote   # 跟踪各子模块分支最新提交
     └─ 失败则降级：git submodule update --recursive（用父仓记录的固定 commit）
  6. 若 -s 指定了 模块=commit：进入子模块目录 git checkout <commit>
```

#### 4.2.3 源码精读

- [build/build.sh:L15-L19](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/build/build.sh#L15-L19) —— 用 bash 关联数组定义「模块名 → 上游 git 仓库」的映射表（`GIT_PATH_OF_MODULE`），四个组件分别指向 gitee.com/omniai 下的四个独立仓库。**精读提示**：这个数组在整个脚本里其实没有被再次消费——真正执行 `git submodule add` 时用的 url 是从 `.gitmodules` 里 `git config -f` 读出来的（见 L82-86）。所以这张表更像是「给人看的文档」+「给 `.gitmodules` 做备份说明」，帮你一眼看穿 components/ 四个目录的来源。

- [build/build.sh:L36-L41](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/build/build.sh#L36-L41) —— `read_all_modules`：用 `grep "\[submodule " .gitmodules` 从 `.gitmodules` 里解析出全部子模块名，存入 `ALL_MODULES` 数组。这就是「有哪些模块可编译」的唯一权威来源——脚本里没有硬编码模块清单（`GIT_PATH_OF_MODULE` 只是映射表）。

- [build/build.sh:L44-L55](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/build/build.sh#L44-L55) —— `check_git`：先确认 git 已安装，再检查 `.gitmodules` 文件是否存在，**不存在直接报错退出**（`log_error ".gitmodules文件不存在"; exit 1`）。注意这个检查发生在 `main` 里且**不受 `-sp`（跳过拉取）开关保护**——这一点在 4.3.3 分析 main 时序时会再强调，它决定了在 gitcode 平铺仓里「只想编译、不想拉码」也绕不开 `.gitmodules` 检查。

- [build/build.sh:L68-L107](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/build/build.sh#L68-L107) —— `init_submodules`：完整实现 4.2.2 的流程。两处值得咀嚼的细节：① L74-81 对已存在的 `components/$mod` 先「备份 .gitmodules → deinit/rm/删除 → 还原 .gitmodules」再重挂，避免旧 submodule 缓存（`.git/modules/...`）污染新克隆；② L92-97 的 `git submodule update --recursive --remote` 带 `--remote` 表示**跟踪子模块分支的最新提交**而非父仓锁定的 commit，失败时降级为不带 `--remote` 的普通 update——「追新优先、锁定兜底」。

#### 4.2.4 代码实践：验证 submodule 机制与当前仓库的真实形态

1. **实践目标**：亲手确认「当前 gitcode 仓没有 `.gitmodules`、组件以普通文件存在」，并理解这对 `build.sh` 意味着什么。
2. **操作步骤**（在本仓根目录执行）：
   ```bash
   # a) 找 .gitmodules —— 预期找不到
   ls -la .gitmodules 2>&1
   # b) 全仓搜索谁引用了 .gitmodules
   grep -rn "gitmodules" --include="*.sh" .
   # c) 干跑顶层构建，观察它在哪一步失败
   bash build/build.sh -m omni-npu
   ```
3. **需要观察的现象**：a) 报 `No such file or directory`；b) 只有 `build/build.sh` 一处引用（read_all_modules/check_git/init_submodules 三个函数）；c) 脚本启动后很快终止——按源码推断，`parse_args` 里的 `read_all_modules`（L39 要 grep `.gitmodules`）或随后的 `check_git`（L51 报「.gitmodules文件不存在」）会触发失败，且因为脚本开头 `set -e`（[L3](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/build/build.sh#L3)），失败即退出。
4. **预期结果**：你会得出结论——**在 gitcode 平铺仓里，顶层 `build.sh` 的「拉子模块」流程无法直接使用**；它是为「部署仓 + .gitmodules」的上游完整形态写的。想在当前仓编译组件，正确姿势是直接调用组件自己的构建脚本（见 4.3.4）。失败的具体报错文案「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：如果上游完整仓库中想把 omni-proxy 钉在某个已验证的 commit `abc1234` 上重新编译，该怎么写命令？

**答案**：`bash build/build.sh -m omni-proxy -s omni-proxy=abc1234`。`-s/--set` 被 `parse_args` 解析后交给 `set_submodule_commit`（[build/build.sh:L57-L65](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/build/build.sh#L57-L65)）记入 `COMMIT_OF_MODULE`，`init_submodules` 在 update 完成后进入 `components/omni-proxy` 执行 `git checkout abc1234`（[L99-L106](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/build/build.sh#L99-L106)）。

**练习 2**：`git submodule update --recursive --remote` 和 `git submodule update --recursive` 的区别是什么？为什么 `init_submodules` 要做失败降级？

**答案**：带 `--remote` 时 git 会先到子模块的远程仓库查询其分支的最新提交并检出它（「永远追最新」）；不带时检出的是**父仓 index 里记录的那个 commit**（「钉死版本」）。降级的原因：`--remote` 需要访问外网 gitee，网络受限环境下会失败，此时退回锁定 commit 至少能保证构建可复现（见 [build/build.sh:L92-L97](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/build/build.sh#L92-L97) 的 `log_warn`「子模块更新失败，尝试仅同步当前提交...」）。

### 4.3 构建入口：build/build.sh 的参数与两级流水线

#### 4.3.1 概念说明

`build/build.sh` 是**顶层构建总入口**，它把「拉子模块」和「编译安装」组织成一条两级流水线：

- **第一级（顶层）**：解析命令行参数，决定「编译哪些模块、要不要拉码、要不要安装」，然后按模块清单逐个分发。
- **第二级（组件）**：每个组件自带一个 `build/build.sh`，真正懂「自己怎么编译」——Python 组件做 `pip install`，C 组件做 `rpmbuild`。顶层脚本对组件的编译细节**一无所知**，它只负责找到子脚本并 `bash` 执行它。

这个「总入口只做调度、子脚本各管各的」设计，就是 4.1 说的 monorepo 构建约定。好处显而易见：新增第五个组件时，顶层 `build.sh` 一行都不用改（模块清单来自 `.gitmodules`），只要新组件遵守「提供 `build/build.sh`」的约定。

#### 4.3.2 核心流程

`main` 的完整时序（[build/build.sh:L282-L307](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/build/build.sh#L282-L307)）：

```text
main "$@"
 ├─ 1. parse_args            # 解析 -m/-s/-sp/-si/-h；未指定 -m 则默认编译全部
 ├─ 2. check_git             # 检查 git 与 .gitmodules（无条件执行！）
 ├─ 3. SKIP_PULL == 0 ?
 │     是 → init_submodules  # 清场重挂 + update（可被 -sp 跳过）
 └─ 4. SKIP_INSTALL == 0 ?
       是 → traverse_submodules
            └─ 对每个模块：check_and_build components/<模块>
                 └─ bash components/<模块>/build/build.sh   # 第二级
                    （失败任一模块 → 立即 exit 1）
```

参数一览（由 `parse_args` 与 `show_help` 定义）：

| 参数 | 含义 | 来源 |
|---|---|---|
| `-m, --modules <a,b,...>` | 只编译列出的模块（逗号分隔）；缺省编译全部 | [L226-L231](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/build/build.sh#L226-L231) |
| `-s, --set 模块=commit` | 把某模块钉到指定 commit | [L234-L242](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/build/build.sh#L234-L242) |
| `-sp, --skip-pull` | 跳过子模块拉取（代码已在本地时） | [L243-L246](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/build/build.sh#L243-L246) |
| `-si, --skip-install` | 跳过编译安装（只想拉码时） | [L247-L250](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/build/build.sh#L247-L250) |
| `-h, --help` | 打印用法 | [L251-L254](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/build/build.sh#L251-L254) |

#### 4.3.3 源码精读

**① parse_args：从命令行到模块清单**

- [build/build.sh:L222-L279](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/build/build.sh#L222-L279) —— `parse_args` 用 `while + case` 逐个吃参数：`-m` 的值用 `IFS=',' read -r -a MODULES_TO_BUILD` 切成数组（L264-268）；随后调用 `read_all_modules` 取得全量模块；若用户没给 `-m`，则打 WARN 并把 `MODULES_TO_BUILD` 设为全部（L272-274）；否则走 `validate_modules`（[L202-L220](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/build/build.sh#L202-L220)）逐个校验模块名是否在 `ALL_MODULES` 里，出现未知模块立即报错退出——所以**模块名的合法取值完全由 `.gitmodules` 决定**（再次呼应 4.2 的发现）。

**② traverse_submodules 与 check_and_build：第二级分发**

- [build/build.sh:L131-L146](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/build/build.sh#L131-L146) —— `traverse_submodules` 的编译循环：对清单里每个模块拼出 `components/<模块>` 路径，存在就交给 `check_and_build`，不存在只 WARN 不退出。
- [build/build.sh:L153-L184](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/build/build.sh#L153-L184) —— **`check_and_build`，本讲实践任务的答案所在**：它把目标脚本硬编码为 `$module_dir/build/build.sh`（L155），存在则 `cd` 进组件目录、补执行权限、`bash "$build_script"` 执行（L158-176），**任何一个模块编译失败立即 `exit 1` 终止整条流水线**（L174-176）；找不到子脚本同样直接退出（L181-182）。这就是「顶层只调度、细节在子脚本」的落地。
- [build/build.sh:L295-L304](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/build/build.sh#L295-L304) —— `main` 里的两个开关判断。注意时序：`check_git`（L289）在 `if SKIP_PULL` 之前**无条件执行**——所以 `-sp` 只能跳过「拉码」，跳不过「`.gitmodules` 必须存在」这道门。

**③ 第二级：四个组件的 build/build.sh 对比**

同一个约定（`build/build.sh`），四种完全不同的产物：

| 组件 | 子脚本核心动作 | 产物 / 安装方式 |
|---|---|---|
| omni-npu | `pip install -e . "$@"`（5 行脚本） | pip 包，可编辑安装 |
| omni-cache | 选 python/venv → 装运行依赖 → `pip install -e . --no-build-isolation`（触发 C++ 原生扩展编译） | pip 包 + 原生扩展 |
| omni-eplb | 检查依赖 → 探测 CANN/NPU 环境 → cmake 编 C++ 测试 → pytest → `pip install -e .` | pip 包（omni_placement）+ C++/gtest |
| omni-proxy | 下载 nginx 1.28.0 源码 → 打 tarball → `rpmbuild` → `rpm -Uvh` 安装 | nginx 动态模块 RPM 包 |

其中 omni-npu 的子脚本短到可以全文引用——

- [components/omni-npu/build/build.sh:L3-L5](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/build/build.sh#L3-L5) —— 用 `realpath` 定位脚本自身所在目录的上一级（即 `components/omni-npu`），`cd` 过去后执行 `pip install -e . "$@"`。`-e` 是 editable（可编辑）安装：容器里 import 的就是这个目录的源码，改代码立即生效，无需重装——这正是顶层 README「推理代码适配」一节让你进容器改代码的物质基础。`"$@"` 会把顶层传来的额外参数透传给 pip。

- [components/omni-proxy/build/build.sh:L21-L46](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/build/build.sh#L21-L46) —— 与 omni-npu 形成鲜明对比：若本地没有 nginx 源码包就 `wget` 下载；随后把 `omni_proxy` 源码目录打成 tarball、连同 spec 文件一起塞进 rpmbuild 目录树，`rpmbuild -ba` 构建 RPM，最后 `yum remove global-proxy && rpm -Uvh` 装进系统。C 组件的「编译」是系统级软件包构建，而不是 pip——这也是练习中「改 omni-proxy 后怎么办」答案的依据。

- [components/omni-eplb/build/build.sh:L36-L67](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/build/build.sh#L36-L67) —— `source_cann_env`：source `~/.bashrc` 后检查/推导 `ASCEND_TOOLKIT_HOME`（优先用已设置的；否则探测 `/usr/local/Ascend/ascend-toolkit/latest` 等路径）。这说明 omni-eplb 的构建强依赖昇腾 CANN 环境，与 omni-npu「纯 Python、5 行搞定」形成梯度——组件越贴近硬件，构建脚本越重。

#### 4.3.4 代码实践：写出「只编译 omni-npu」的命令（本讲主实践）

1. **实践目标**：完成讲义规格中的任务——基于对 `parse_args` 与 `check_and_build` 的阅读，写出只编译 omni-npu 一个模块的完整命令，并**预测**它会调用哪个子目录下的脚本、该脚本做什么。
2. **操作步骤**：
   - 第一步，写出命令。依据 `show_help` 的示例（`$0 --modules module2`）与 `parse_args` 的解析逻辑，命令为：
     ```bash
     # 在仓库根目录执行
     bash build/build.sh -m omni-npu
     # 等价写法
     bash build/build.sh --modules omni-npu
     ```
   - 第二步，在源码中逐行追踪这条命令的路径，验证你的预测（纯阅读，不执行）：
     1. `parse_args`（L222）把 `omni-npu` 存入 `MODULES_TO_BUILD` 数组；
     2. `read_all_modules` 取得全量模块，`validate_modules` 确认 `omni-npu` 合法；
     3. `check_git` 检查 `.gitmodules`；
     4. `init_submodules` 拉取 `components/omni-npu`（上游完整仓形态）；
     5. `traverse_submodules` → `check_and_build "components/omni-npu"`。
   - 第三步，给出预测并核对：依据 [build/build.sh:L155](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/build/build.sh#L155) 的拼接规则 `build_script="$module_dir/build/build.sh"`，被调用的脚本是 **`components/omni-npu/build/build.sh`**；它只有 5 行，进入 `components/omni-npu` 后执行 `pip install -e .`。
   - 第四步（可选，本地为 gitcode 平铺仓时）：由于本仓没有 `.gitmodules`，第三步的完整链路会卡在 `check_git`。此时可以**直接调用第二级脚本**验证预测的后半段：
     ```bash
     cd components/omni-npu && bash build/build.sh
     ```
3. **需要观察的现象**：源码追踪中每一步对应的日志前缀（`[INFO]`/`[WARN]`/`[ERROR]`，由 L23-33 的三个 log 函数输出）：`将编译以下模块: omni-npu` → `---Step1：初始化子模块...---` → `---Step3：遍历子模块执行编译...---` → `找到 build.sh，开始编译安装: components/omni-npu` → `编译安装成功: components/omni-npu`。直接跑第二级脚本时应看到 pip 的 editable 安装输出。
4. **预期结果**：命令 `bash build/build.sh -m omni-npu`；预测调用 `components/omni-npu/build/build.sh`；该脚本执行 `pip install -e .`（依赖 vllm==0.14.0 与 torch_npu 已就位，见 [components/omni-npu/README.md:L15-L29](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/README.md#L15-L29) 的安装顺序说明）。在 gitcode 平铺仓上完整链路的实际表现「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`bash build/build.sh -m omni-npu,omni-cache` 中某个模块编译失败了，脚本会继续编译下一个吗？

**答案**：不会。`check_and_build` 里编译失败走 `log_error` 后直接 `exit 1`（[build/build.sh:L170-L176](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/build/build.sh#L170-L176)，注释原文「失败1个即退出」），加上脚本开头 `set -e`，整条流水线立即终止——这是刻意选择的「fail fast」策略，避免用旧产物拼出不一致的运行环境。

**练习 2**：你改了 omni-npu 的代码，想让它在容器里生效。A 方案：重跑 `bash build/build.sh -m omni-npu`；B 方案：进容器找到 omni-npu 的安装路径直接改文件。哪个立即生效？为什么？

**答案**：B 立即生效（前提是最初以 `pip install -e .` 可编辑模式安装，import 时直接读源码目录）；A 重新走一遍完整流水线（含 submodule 拉取），更适合在宿主机上固化修改。顶层 README 的「推理代码适配」一节（[README.md:L118-L125](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README.md#L118-L125)）描述的正是 B 方案。注意 omni-proxy 不适用此法——它是 RPM 安装的 C 模块（见 4.3.3 ③）。

**练习 3**：`-sp`（跳过拉取）和 `-si`（跳过安装）分别把 `main` 流程裁剪成什么样？

**答案**：`-sp` 跳过 `init_submodules`（L295-297 不成立），流程变成「解析参数 → check_git → 直接编译」，适合代码已在本地、只改了代码想快速重装的场景；`-si` 跳过 `traverse_submodules`（L302-304 不成立），流程变成「解析参数 → check_git → 只拉码不编译」，适合只想把四个组件的源码按 `.gitmodules` 同步下来的场景。两者都**跳不过** `check_git` 的 `.gitmodules` 检查。

## 5. 综合实践

**任务：为你的团队写一页《新同事仓库上手地图》。** 把本讲三个模块串起来，产出一份 markdown 文档，包含以下四部分（全部基于你本机的真实命令输出，而非本讲义的文字）：

1. **目录速览**：执行 `git ls-files | cut -d/ -f1-2 | sort | uniq -c | sort -rn | head -20`，把输出整理成 annotated 目录树，标注每个目录「运行组件 or 部署工具」及其一句话职责。
2. **组件档案卡**：为四个组件各建一张卡：上游仓库地址（抄自 `build/build.sh` 的 `GIT_PATH_OF_MODULE`）、构建产物类型（pip 包 / RPM，依据各自 `build/build.sh` 的核心命令）、在本仓是否为平铺普通文件（依据 `git ls-files components/<模块> | wc -l` 是否大于 1）。
3. **构建流水线图**：画 `bash build/build.sh -m <模块>` 的流程图（parse_args → check_git → init_submodules → check_and_build → 子脚本），在 `check_git` 节点旁标注你实测的结论（本仓缺 `.gitmodules` 时会发生什么），并给出本仓可用的替代命令（直接调 `components/<模块>/build/build.sh`）。
4. **一句话 FAQ**：回答三个高频问题——「我只改部署配置该动哪里？」（tools/ansible）、「我改推理代码该动哪里、怎么生效？」（components/ + pip -e 或进容器改）、「为什么顶层 build.sh 报 .gitmodules 不存在？」（gitcode 平铺形态 vs 上游 submodule 形态）。

完成后自检：文档里每个结论都能指回一条你亲自执行过的命令或一处 `build/build.sh` 的行号。

## 6. 本讲小结

- 仓库是「部署文档 + `build/` 构建入口 + `components/` 四大运行组件 + `tools/` 部署工具链」的 monorepo；**`components/` 影响推理行为，`tools/` 影响部署方式**。
- 四个组件（omni-npu / omni-cache / omni-eplb / omni-proxy）各自对应一个独立的 gitee 仓库，顶层通过 git submodule 机制把它们挂到 `components/` 下，`build/build.sh` 负责拉取、锁定 commit 并统一编译。
- **关键事实**：当前 gitcode 开源仓中没有 `.gitmodules`，组件源码被平铺为本仓普通文件；顶层 `build.sh` 的 submodule 流程是为上游完整形态设计的，在本仓直接运行会卡在 `check_git` 的 `.gitmodules` 检查（可用组件自带的 `build/build.sh` 替代）。
- 构建是两级流水线：顶层 `build.sh` 只做「解析 `-m/-s/-sp/-si` → 拉码 → 逐模块分发」，真正的编译细节在每个组件的 `build/build.sh` 里——同一个约定，四种产物（pip 包 ×3、RPM ×1），任一模块失败即整体终止（fail fast）。
- 只编译 omni-npu 的命令是 `bash build/build.sh -m omni-npu`，它最终调用 `components/omni-npu/build/build.sh`，也就是一句 `pip install -e .`（可编辑安装，改代码即生效）。

## 7. 下一步学习建议

下一讲 [u1-l3 模型规格与部署拓扑](u1-l3-models-and-topologies.md) 将深入 `tools/ansible/92B` 与 `tools/ansible/505B`，逐字段解读 inventory 中 P、D、C 节点的定义（`node_rank`、`kv_rank`、`port_offset` 等）——即本讲目录树里 `tools/ansible` 那一坨文件的内部结构。

如果想提前热身，推荐先读：

- [tools/ansible/92B/omni_infer_inventory_used_for_1P1D.yml](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_inventory_used_for_1P1D.yml) —— 最小的节点清单，感受 inventory 长什么样。
- [build/build.sh](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/build/build.sh) 全文再通读一遍（313 行），这次重点看 `show_help` 的示例与 `validate_modules` 的校验循环，巩固 bash 关联数组与数组的用法。
