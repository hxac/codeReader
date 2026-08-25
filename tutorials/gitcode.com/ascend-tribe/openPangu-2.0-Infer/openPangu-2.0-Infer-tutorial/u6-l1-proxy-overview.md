# Omni Proxy 架构与快速上手

> 单元六 · 第 1 讲（u6-l1）｜学习阶段：intermediate｜依赖：u1-l4（用 ansible 拉起第一个 1P1D BF16 服务）

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 omni-proxy 为什么以及如何以 **Nginx 动态模块** 的方式存在——它不是一个新的 HTTP 服务器，而是寄生在标准 Nginx 进程模型上的一组 `.so` 扩展。
2. 复述推理请求在 proxy 视角下的 **10 阶段生命周期**，并说出每个阶段对应 access log 里的哪个字段。
3. 区分 **sequential** 与 **parallel** 两种 PD 调度模式的差异、各自适配的推理框架（vLLM / SGLang），以及它们与 KV Cache 传输时机的关系。
4. 掌握 `omni_proxy.sh` 的参数体系与配置生成流水线：约 40 个命令行参数如何变成一份可被 `nginx -t` 校验的 `nginx.conf`。

承接前文：u1-l4 中我们用 `run_proxy` 这个 tag 在 C 节点拉起了 nginx + proxy 并监听 7000 端口，但当时把 proxy 当作黑盒；本讲把这个黑盒的架构、调度模型与配置面打开。C 源码的逐行精读留给下一讲（u6-l2），ansible 侧的接入方式留给 u6-l3。

## 2. 前置知识

阅读本讲前，你需要理解以下几个基础概念（已学过 u1-l1～u1-l5 的话，前两条是复习）：

- **PD 分离与 C 节点**：openPangu-2.0-Infer 把 Prefill（计算密集）与 Decode（访存密集）部署在不同节点，KV Cache 跨节点传输。C 节点跑 nginx + omni-proxy，作为客户端唯一入口（默认 7000 端口）。
- **upstream（上游）**：Nginx 术语，指一组被代理的后端服务器。omni-proxy 里有两个关键 upstream：`prefill_endpoints`（P 节点 API 端口列表）和 `decode_endpoints`（D 节点 API 端口列表）。还记得 u1-l3 的端口三级体系吗？proxy 转发用的正是 9000 段的 api_port。
- **Nginx 进程模型**：1 个 master 进程负责读配置、管理 worker；N 个 worker 进程用 epoll 事件循环处理请求。`worker_processes`、`worker_cpu_affinity`、`reuseport` 都是围绕这个模型的调优参数。
- **Nginx 动态模块（dynamic module）**：Nginx 允许把第三方功能编译成独立的 `.so` 共享库，在 `nginx.conf` 顶部用 `load_module` 指令加载。模块可以注册**自定义配置指令**（如 omni-proxy 的 `omni_proxy`、`omni_proxy_pd_policy`），也能拦截请求处理流程。这是"不改 Nginx 源码扩展 Nginx"的标准机制——和 omni-npu 用插件机制不改 vLLM 源码（u2-l1）是同一种工程哲学。
- **为什么大模型推理需要专用 proxy**：传统负载均衡只看连接数/CPU，而推理调度的关键信号是 token 数、KV Cache 命中率、批次执行周期等"推理内生指标"。README 把这归结为四大挑战：周期性负载、性能感知缺失、KV Cache 精准匹配难题、P/D 重复 tokenize 的冗余计算。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [components/omni-proxy/omni_proxy/README_CN.md](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/README_CN.md) | 中文架构文档：请求 10 阶段生命周期、双调度模式、APC 感知调度、快速上手命令 |
| [components/omni-proxy/omni_proxy/README.md](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/README.md) | 英文文档：与第一代 Global Proxy 的对比、三条调度核心原则 |
| [components/omni-proxy/omni_proxy/omni_proxy.sh](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/omni_proxy.sh) | 约 700 行的 bash 启动脚本：解析参数 → 生成 nginx.conf → 管理 nginx 生命周期 |
| [components/omni-proxy/omni_proxy/nginx.conf](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/nginx.conf) | 一份手写示例配置，展示生成配置的"最终形态"长什么样 |
| [components/omni-proxy/omni_proxy/build.sh](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/build.sh) | 编译脚本：下载 Nginx 1.28.0 源码并以 `--add-dynamic-module` 方式编译出两个 `.so` |
| [components/omni-proxy/omni_proxy/modules/config](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/config) | Nginx 模块构建描述文件：声明模块名、11 个 C 源文件与 Python/ZeroMQ/msgpack 三个外部依赖 |

另外，`modules/` 目录下还有约 30 个 C 源文件与头文件（`omni_scheduler.c`、`omni_apc.c`、`omni_radix_tree.c`、`omni_tokenizer.c` 等），它们是动态模块的主体，本讲只建立地图，精读留给 u6-l2。

## 4. 核心概念与源码讲解

### 4.1 Nginx 动态模块：omni-proxy 如何"寄生"在 Nginx 上

#### 4.1.1 概念说明

omni-proxy 是 Omni Infer 的第二代请求调度引擎，官方称其在 Omni Infer 0.3.0 版本中带来了超过 10% 的推理性能提升（见 [README_CN.md:8](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/README_CN.md#L8)）。

它的第一个架构决策是：**不自己写 HTTP 服务器，而是扩展 Nginx**。好处有三：

1. 白得一个工业级事件驱动内核：epoll、`reuseport` 多 worker 接收、CPU 亲和绑核、连接复用。
2. 与标准 Nginx 兼容，可以动态模块加载、无缝集成到已有部署（[README_CN.md:108-109](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/README_CN.md#L108-L109)）。
3. 调度行为可以表达成 **Nginx 配置指令**（`omni_proxy_pd_policy sequential;` 这类），运维侧零新语法成本。

omni-proxy 实际编译出**两个**动态模块：

- `ngx_http_omni_proxy_module.so`：主模块，包含调度器、APC、tokenizer、ZMQ 订阅等全部逻辑；
- `ngx_http_set_request_id_module.so`：辅助模块，为每个请求生成唯一 request id（用于日志串联与链路追踪）。

#### 4.1.2 核心流程

动态模块从源码到运行的生命周期：

1. **编译期**：`build.sh` 下载 Nginx 1.28.0 官方源码 tar 包，执行 `./configure --add-dynamic-module=<modules 目录>`，Nginx 构建系统读取该目录下的 `config` 文件，把 11 个 C 文件编进一个 `.so`，`make install` 后落到 `/usr/local/nginx/modules/`。
2. **加载期**：nginx master 进程启动时读取 `nginx.conf`，遇到顶部的 `load_module` 指令就用 dlopen 加载 `.so`，模块注册的所有自定义指令从此可用。
3. **运行期**：master 按 `worker_processes` fork 出 N 个 worker，每个 worker 继承已加载的模块代码，用 epoll 处理请求；omni-proxy 在请求处理的各个钩子上插入调度逻辑。

`modules/config` 还揭示了主模块的三个外部依赖，每个都对应一块功能：

| 依赖库 | 用途推测依据 |
| --- | --- |
| Python 3.11（libpython） | C 模块内嵌调用 Python tokenizer（`omni_tokenizer.c` + `omni_tokenizer_worker.c` + `omni_tokenizer.py`） |
| ZeroMQ（libzmq） | `omni_zmq_handler.c` 订阅 vLLM 推理引擎广播的 KV Cache 事件 |
| msgpack-c | 二进制序列化，配合 ZMQ 消息编解码 |

#### 4.1.3 源码精读

编译入口在 [components/omni-proxy/omni_proxy/build.sh:106-116](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/build.sh#L106-L116)：两次 `--add-dynamic-module` 分别编译主模块与 request-id 模块，`--without-http_gzip_module` 去掉推理转发用不上的 gzip，最后把 `omni_tokenizer.py` 软链进 Python site-packages 并导出 `PYTHONHASHSEED=123`：

```bash
cd nginx-${NGINX_VERSION}
CFLAGS="-O0 -g $COVERAGE_FLAGS" ./configure --sbin-path=${NGINX_SBIN_PATH} \
    --add-dynamic-module=$WORKDIR/omni_proxy/modules \
    --add-dynamic-module=$WORKDIR/omni_proxy/modules/ngx_http_set_request_id_module \
    --without-http_gzip_module \
    --with-ld-opt="$COVERAGE_FLAGS"
make -j16
make install
```

模块构建描述文件 [components/omni-proxy/omni_proxy/modules/config:10-21](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/config#L10-L21) 列出主模块的 11 个 C 源文件——这份清单就是 omni-proxy 的功能地图：

```bash
ngx_module_srcs="$ngx_addon_dir/ngx_http_omni_proxy_module.c \
                 $ngx_addon_dir/omni_pd_body_rewrite.c \
                 $ngx_addon_dir/omni_scheduler.c \
                 $ngx_addon_dir/omni_utils.c \
                 $ngx_addon_dir/omni_tokenizer.c \
                 $ngx_addon_dir/omni_tokenizer_worker.c \
                 $ngx_addon_dir/omni_radix_tree.c \
                 $ngx_addon_dir/omni_apc.c \
                 $ngx_addon_dir/omni_health.c \
                 $ngx_addon_dir/omni_metrics.c \
                 $ngx_addon_dir/omni_zmq_handler.c"
```

文件名与功能一一对应：`scheduler` 调度、`apc` + `radix_tree` 前缀缓存匹配、`tokenizer*` 分词、`zmq_handler` 事件订阅、`metrics`/`health` 可观测性、`pd_body_rewrite` 请求体改写。

加载侧，示例配置 [components/omni-proxy/omni_proxy/nginx.conf:1-2](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/nginx.conf#L1-L2) 的前两行就是 `load_module`：

```nginx
load_module /usr/local/nginx/modules/ngx_http_omni_proxy_module.so;
load_module /usr/local/nginx/modules/ngx_http_set_request_id_module.so;
```

而 `omni_proxy.sh` 生成配置时会原样写入这两行，见 [components/omni-proxy/omni_proxy/omni_proxy.sh:473-474](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/omni_proxy.sh#L473-L474)。

Nginx 进程模型相关的两行也值得一看：[nginx.conf:10-12](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/nginx.conf#L10-L12) 里 `worker_processes 4` 开 4 个 worker，`worker_cpu_affinity 1 10 100 1000` 用二进制掩码把 4 个 worker 分别钉在 CPU 0/1/2/3 上——每 worker 独占一核，避免调度抖动。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：不看正文，仅凭 `modules/config` 与 `build.sh` 说出 omni-proxy 由哪两个模块组成、各含哪些源文件、依赖哪三个外部库。
2. **操作步骤**：
   - 阅读 [modules/config](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/config) 全文，特别注意 26-46 行的 `detect_python_config`、48-72 行的 `detect_zeromq_config`、74-88 行的 `detect_msgpack_config` 三个探测函数。
   - 用 `ls components/omni-proxy/omni_proxy/modules/` 对照清单，找出清单之外还有哪些文件（如 `omni_shared_state.h`、`jsmn.h`），并推测其角色。
3. **需要观察的现象**：`config` 文件里 `ngx_module_srcs` 只列了 11 个 `.c`，而目录下 `.h` 文件更多——头文件不必单独编译，`jsmn.h` 是第三方单头 JSON 解析库。
4. **预期结果**：能画出"11 个 C 文件 → 1 个 ngx_http_omni_proxy_module.so + 独立的 set_request_id 模块 → load_module 加载"的链路图。
5. 本实践为纯源码阅读，无需运行环境。

#### 4.1.5 小练习与答案

**练习 1**：为什么 omni-proxy 选择动态模块（`--add-dynamic-module`）而不是直接改 Nginx 源码（静态编译进去）？

**答案**：动态模块让 omni-proxy 可以跟随任意官方版 Nginx（这里是 1.28.0）使用，升级 Nginx 不需要维护源码 patch；`.so` 独立发布、独立替换，与 omni-npu 之于 vLLM 的 out-of-tree 插件思路一致——核心引擎保持原版，扩展走旁路。

**练习 2**：`worker_cpu_affinity 1 10 100 1000` 中 `10` 和 `1000` 分别是什么意思？

**答案**：这是 4 个二进制掩码，分别写作 `0001`、`0010`、`0100`、`1000`，即把 4 个 worker 进程依次绑定到 CPU 0、1、2、3 号核。`omni_proxy.sh` 中的 `gen_affinity_masks` 函数（[omni_proxy.sh:354-372](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/omni_proxy.sh#L354-L372)）就是按 `--start-core-index` 与 `--core-num` 自动生成这串掩码的。

**练习 3**：`build.sh` 末尾为什么要 `export PYTHONHASHSEED=123`？

**答案**：C 模块内嵌的 Python tokenizer 要计算 block hash 用于 APC（前缀缓存）匹配；Python 的字符串哈希默认每次进程随机化，若 proxy 侧与推理引擎侧哈希种子不一致，同一 prompt 会算出不同的 hash，缓存永远匹配不上。固定 `PYTHONHASHSEED` 保证两侧哈希结果一致（README 快速上手一节同样要求设置，见 [README_CN.md:125-127](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/README_CN.md#L125-L127)）。

### 4.2 请求 10 阶段生命周期与 sequential/parallel 双调度模式

#### 4.2.1 概念说明

**（1）10 阶段生命周期**。要调度请求，先要能度量请求。omni-proxy 从"请求级调度引擎"的视角把一个推理请求切成 10 个阶段（[README_CN.md:31-47](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/README_CN.md#L31-L47)）：

| # | 阶段 | 含义 |
| --- | --- | --- |
| 1 | 接收到请求 | 拿到请求体 |
| 2 | Tokenize | prompt 文本 → token id 列表，含 chat template 模板展开 |
| 3 | APC matching | 基于 KV Cache 前缀缓存做 upstream 寻优 |
| 4 | Prefill waiting | 预填充等待调度，按各 upstream 的调度周期卡点 |
| 5 | Prefill scheduled | 调度器已选定 P 节点，等 Nginx worker 执行调度结果 |
| 6 | Prefill running | 预填充执行中 |
| 7 | Decode waiting | 等待 Decode 调度 |
| 8 | Decode scheduled | 解码已调度 |
| 9 | Decode running | 解码执行中 |
| 10 | 请求完成 | 响应返回 |

每个阶段都埋了性能采集点，多 worker 之间通过 **Nginx 共享内存**同步数据（第 47 行）。这套阶段模型不是文档摆设——4.1 里 access log 的字段（`tknized`、`wait_p`、`p_sched`、`wait_d`……）就是这些阶段的直接可观测输出。

**（2）sequential / parallel 双调度模式**。PD 分离下 KV Cache 要从 P 传到 D，"什么时候选 D 节点、什么时候分配 D 侧接收 blocks"存在两种做法（[README_CN.md:49-60](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/README_CN.md#L49-L60)）：

- **sequential（先 P 后 D）**：P 先完成推理，请求再发给 D；D 按需分配接收 blocks，分配完成后才从 P 拉取 KV Cache。优点是**延迟绑定**——选 D 时能看到最新负载；缺点是拉 KV 的时间难以被并行掩盖。vLLM 是这种实现，本仓库的部署默认也是它。
- **parallel（P/D 同步选）**：调度器同时选 P 和 D，D 先预分配好 KV 传输目的 blocks，然后才开始 Prefill；P 在推理过程中**按层**把生成的 KV 推给 D，Prefill 一结束 D 立刻能进下一个推理 batch。传输被计算掩盖，吞吐更好，但必须提前锁定 D。SGLang 是这种实现。

你可以对照 u4-l2 的 LLMDataDistConnector 验证：D 侧 `pull_blocks` 主动拉取、P 侧"收到回执或 600 秒超时才释放"，正是 sequential 模式在引擎侧的配套行为。

**（3）调度算法要点**（[README_CN.md:84-96](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/README_CN.md#L84-L96)）：请求先按"token 长度 + 等待时间"加权排序，越小、等得越久的越先分发；为防止大 prompt 饿死，设有等待时间阈值。选节点时先看 APC 匹配，再做负载均衡（选当前负载 token 数最小的 upstream），并带过载保护与基于周期预测的精准分发。英文 README 补充了 decode 侧策略：用"prompt + 预期输出"估计总负载，采用**最长处理时间优先（LPT）**以减少执行空洞（[README.md:33-37](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/README.md#L33-L37)）。

#### 4.2.2 核心流程

sequential 模式下一次请求的完整流转（阶段编号对应上表）：

```text
客户端 ──HTTP──▶ proxy(7000)
  │ ①接收 ②Tokenize(proxy 内完成，含 chat template 展开)
  │ ③APC matching：radix 树查各 P 节点前缀缓存 → 选出命中率最高的候选
  │ ④Prefill waiting：按排序权重排队，卡着 P 的调度周期等空位
  │ ⑤Prefill scheduled → ⑥转发到选定的 P，P 计算 prefill
  ▼
P 节点完成 prefill（KV Cache 留在 P 侧，等 D 来取）
  │ ⑦Decode waiting：此刻才根据 D 池最新负载选 D
  │ ⑧Decode scheduled → 请求转发到 D
  │    D：分配接收 blocks → 从 P 拉取 KV Cache（LLMDataDistConnector）
  ▼ ⑨Decode running：D 逐 token 生成，流式回传
 ⑩请求完成，各阶段耗时写入 access log
```

parallel 模式只改一处：第 ⑤ 步选 P 的**同时**就选好 D 并预分配目的 blocks，P 一边算一边按层推 KV，⑥ 一结束 D 直接进入 ⑨。

请求排序可概念化为（**示意公式**，README 未公开具体函数形式，实现在 `omni_scheduler.c`，u6-l2 精读）：

\[ \text{priority}(r) \;\approx\; \alpha \cdot \text{len}_{\text{token}}(r) \;-\; \beta \cdot \text{wait}(r) \]

即 token 越少、等待越久，优先级越高；等待超过 starvation 阈值（配置项 `omni_proxy_prefill_starvation_timeout`，默认 400）的请求被强制提升，避免饿死。

#### 4.2.3 源码精读

10 阶段定义在 [README_CN.md:34-45](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/README_CN.md#L34-L45)（ numbered list 1-10）。两种调度模式的原文描述在 [README_CN.md:52-60](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/README_CN.md#L52-L60)。

阶段模型落到代码里的证据是 access log 格式。[omni_proxy.sh:389-433](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/omni_proxy.sh#L389-L433) 的 `gen_access_log` 函数生成一份 JSON 日志格式，摘取关键片段：

```nginx
'"promt_tks":"$promt_tks",'
'"decoded_tks":"$decoded_tks",'
'"prefill_max_match":"$prefill_max_match",'
'"decode_max_match":"$decode_max_match",'
'"prefill_idx":"$prefill_idx",'
'"decode_idx":"$decode_idx",'
'"tknized":"$tknized",'
'"apc":"$apc",'
'"wait_p":"$wait_p",'
'"p_sched":"$p_sched",'
'"to_p":"$to_p",'
'"p_ed":"$p_ed",'
'"wait_d":"$wait_d",'
'"d_sched":"$d_sched",'
'"to_d":"$to_d",'
'"1st_tk":"$1st_tk",'
'"tpot":"$tpot",'
'"ttft":"$ttft",'
```

这些 `$` 开头的变量是 C 模块在请求处理各阶段写入的 Nginx 变量——`tknized` 对应阶段②耗时、`apc` 对应阶段③、`wait_p`/`p_sched`/`to_p`/`p_ed` 对应阶段④⑤⑥、`wait_d`/`d_sched`/`to_d` 对应阶段⑦⑧，`ttft`（首 token 时间）与 `tpot`（每输出 token 平均时间）则是端到端指标。

调度模式本身是一个配置指令。生成配置时写入 [omni_proxy.sh:536](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/omni_proxy.sh#L536)：

```nginx
omni_proxy_pd_policy $omni_proxy_pd_policy;
```

取值在参数解析处做了白名单校验（[omni_proxy.sh:150-157](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/omni_proxy.sh#L150-L157)），除了 sequential/parallel 还接受第三个值 `aggregation`——该模式下**不生成** `prefill_endpoints` upstream（[omni_proxy.sh:510-512](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/omni_proxy.sh#L510-L512) 的条件判断），也不要求提供 prefill 端点（[omni_proxy.sh:659-662](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/omni_proxy.sh#L659-L662)），即 proxy 只面向 decode 上游工作、不做 PD 编排（README 未展开，本讲点到为止）。

另外两项与本模块强相关的能力：

- **Tokenizer 结果复用**（[README_CN.md:75-82](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/README_CN.md#L75-L82)）：proxy 在转发前完成模板展开与分词，把结果附进请求体，下游引擎不再重复 tokenize——多机 PD 分离场景可省约 30% 的 tokenizer 开销。这正是阶段②"寄生"在 proxy 里的原因。
- **主从调度**（[README_CN.md:98-105](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/README_CN.md#L98-L105)）：多个 Nginx worker 中选举一个主调度器做全局决策，调度结果经共享内存同步给其他 worker；各 worker 本地采指标、用原子操作更新共享内存，避免锁竞争。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：把 access log 字段与 10 阶段生命周期一一对应，验证"阶段模型是可观测的"。
2. **操作步骤**：
   - 通读 [omni_proxy.sh:389-433](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/omni_proxy.sh#L389-L433) 的 `gen_access_log`，抄下所有非 Nginx 内置的变量（凡是与请求阶段相关、Nginx 原生没有的，都是 C 模块注入的）。
   - 对照本节 10 阶段表格，为每个阶段标注"耗时字段 + 上游编号字段"两列。例如阶段⑤⑨对应 `p_sched`/`d_sched` 与 `prefill_idx`/`decode_idx`。
   - 若你已完成 u1-l4 部署：`docker exec` 进 C 节点容器，`tail -f` access log（路径见启动参数 `--access-log-file`，默认 `/tmp/nginx_access.log`），发一条流式请求，观察一行 JSON 里各字段的值。
3. **需要观察的现象**：一条真实请求的日志行里 `ttft` ≈ `tknized + apc + wait_p + to_p + p_ed + wait_d + to_d + 1st_tk` 的量级组合；`prefill_idx` 与 `decode_idx` 指向不同的 upstream 地址。
4. **预期结果**：得到一张"阶段 ↔ 日志字段"映射表。无部署环境时，仅完成前两步的纯阅读部分即可，第三步标注"待本地验证"。

#### 4.2.5 小练习与答案

**练习 1**：sequential 模式"延迟选择 D 节点"的好处是什么？代价是什么？

**答案**：好处是调度器在第⑦步才决定 D，此刻能看到 D 池最新负载，可以把请求分给最空闲/缓存最合适的 D 节点；代价是 D 分配 blocks 后才开始拉 KV Cache，这段传输时间串行暴露在关键路径上，无法被 Prefill 计算掩盖。

**练习 2**：为什么 parallel 模式能"Prefill 一结束 D 立刻进入下一轮 batch"？

**答案**：parallel 模式在 Prefill 开始前就完成了 D 的选定与目的 blocks 预分配，P 在推理过程中按层把生成的 KV 推送到 D；Prefill 结束时 D 侧 KV 已经就位，无需再等待传输，可直接加入下一个 decode batch。本质是用"提前承诺 D 节点"换"传输与计算重叠"。

**练习 3**：请求排序为什么要引入"等待时间阈值"机制？

**答案**：排序权重偏向小请求（token 少者优先），大 prompt 请求权重持续偏低，可能长时间得不到调度（饥饿）；阈值机制让等待超过 `omni_proxy_prefill_starvation_timeout`（默认 400，单位见配置说明）的请求获得高优先级强制分发，在吞吐（优先小请求）与公平性（不饿死大请求）之间取得平衡。

### 4.3 omni_proxy.sh：从部署参数到 nginx.conf 的配置生成流水线

#### 4.3.1 概念说明

`omni_proxy.sh` 是 omni-proxy 的唯一启动入口，约 700 行 bash，做三件事：

1. **参数解析**：约 40 个长参数（`--prefill-endpoints`、`--omni-proxy-pd-policy` 等），每个都有默认值；
2. **配置生成**：把参数渲染进一份 nginx.conf 模板（heredoc），条件化地生成 upstream 块与指令；
3. **生命周期管理**：启动 / 停止（`--stop`）/ 热加载（`--reload`）/ 回滚（`--rollback`）/ 只生成不启动（`--dry-run`）/ 保留存量 nginx（`--keepalive-nginx`）。

设计思想：**部署参数不应该让人手改 nginx.conf**。u1-l4 里 ansible 的 `run_proxy` tag 最终拼出的就是一条 `omni_proxy.sh` 命令（u6-l3 详述），于是"改调度行为 = 改 ansible 变量 = 改脚本参数"，整条链路可审计、可回滚（生成前自动备份旧配置为 `_bak` 文件）。

#### 4.3.2 核心流程

脚本主干流程（自底向上读）：

```text
main()                              # L685-693 三分发
 ├─ --reload   → do_reload()        # 重新生成配置 → nginx -t 校验 → nginx -s reload（失败自动回滚）
 ├─ 默认       → do_start()         # 核心启动路径
 │    ├─ set_nginx_env_defaults()   # PYTHONHASHSEED / TORCH_DEVICE_BACKEND_AUTOLOAD 兜底
 │    ├─ 校验 --decode-endpoints 必填；非 aggregation 时 --prefill-endpoints 必填
 │    ├─ generate_nginx_conf()      # 渲染 nginx.conf
 │    ├─ --dry-run → 到此为止，退出
 │    ├─ stop_nginx()               # 循环 kill -15 直到进程清空（--keepalive-nginx 跳过）
 │    └─ start_nginx()              # nginx -c <conf>
 └─ --stop     → do_stop()          # 停 nginx，可选回滚 _bak
```

`generate_nginx_conf`（L435-649）内部的渲染顺序：

1. `gen_affinity_masks` 算出每个 worker 的 CPU 掩码；
2. 已有配置先备份为 `<conf>_bak`（`cp -n`，不覆盖已有备份）；
3. 各"可选指令"先拼成字符串（分组、调度算法、max_tokens_weight），非空才写入；
4. heredoc 写主模板：`load_module` ×2 → `env` 白名单 → `worker_processes`/`worker_cpu_affinity` → `events` → `http`（含 access log、超时、upstream 块）；
5. `listen` 行按 `--no-reuseport` 决定是否带 `reuseport`；
6. 追加 `location` 块：核心的 `/v1(/chat)?/completions` 正则 location 内堆放全部 `omni_proxy_*` 指令；
7. 若给了 `--omni-proxy-model-path`，追加 `omni_proxy_model_path` 与 `omni_proxy_vllm_kv_port_offset 100`（开启 APC 感知与 tokenizer 复用；该偏移指令的确切语义在 C 模块中使用，留待 u6-l2）。

upstream 块由 [omni_proxy.sh:374-387](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/omni_proxy.sh#L374-L387) 的 `gen_upstream_block` 统一生成。

#### 4.3.3 源码精读

**默认值区**（[omni_proxy.sh:7-45](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/omni_proxy.sh#L7-L45)）：所有可调项先给默认值——监听 7150（部署时 ansible 传 7000）、4 worker、从 0 号核起、`sequential` 策略、`max_batch_num_token` 32000、双端 `max_num_seqs` 32、饥饿阈值 400、读超时 14400s（4 小时，为长生成兜底）。

**upstream 生成**（[omni_proxy.sh:374-387](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/omni_proxy.sh#L374-L387)）：

```bash
block+="        keepalive 2048;\n"
block+="        keepalive_timeout 110s;\n"
block+="        keepalive_requests 20000;\n"
IFS=',' read -ra list <<< "$endpoints"
for addr in "${list[@]}"; do
    block+="        server $addr max_fails=3 fail_timeout=10s;\n"
done
```

逗号分隔的端点列表被拆成多个 `server` 行；长连接池 2048 条（转发推理请求频繁，建连开销必须摊薄）；`max_fails=3 fail_timeout=10s` 是 Nginx 原生的故障摘除——10 秒内失败 3 次的上游暂时拉黑。

**核心 location**（[omni_proxy.sh:531-551](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/omni_proxy.sh#L531-L551)）：

```nginx
location ~ ^/v1(/chat)?/completions$ {
    set_request_id on;
    set_trace_headers_force $set_trace_headers_force;
    omni_proxy decode_endpoints;
    stream_ops $stream_ops;
    omni_proxy_pd_policy $omni_proxy_pd_policy;
    omni_proxy_tokenize_chunk_bytes $omni_proxy_tokenize_chunk_bytes;
    omni_proxy_max_batch_num_token $omni_proxy_max_batch_num_token;
    ...
    prefill_pod_size $prefill_pod_size;
    decode_pod_size $decode_pod_size;
```

三个要点：URL 正则同时匹配 `/v1/completions` 与 `/v1/chat/completions`；`omni_proxy decode_endpoints` 是主指令——把该 location 的流量交给调度引擎、默认上游指向 decode 组；`prefill_pod_size`/`decode_pod_size` 告诉调度器"一个 P/D 实例由几台机器组成"（对应 u1-l3 的多机实例，如 4P81D16 里 8 机的 P 实例）。

**分组指令的规整化**（[omni_proxy.sh:460-470](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/omni_proxy.sh#L460-L470)）：脚本入参用逗号分隔（`"0:2,1:1"`），渲染时替换为空格（`0:2 1:1`）以符合 Nginx 指令语法。分组的语义：`omni_proxy_prefill_groups 0:2 1:1 1:1` 表示 prefill upstream 的前 2 个 server 属于组 0、第 3 个属于组 1、第 4 个属于组 1；请求会被"绑死"在同一个分组内选 P 和 D（P/D 分组 id 必须一一对应，否则启动失败，详见 [README_CN.md:147-153](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/README_CN.md#L147-L153)）。

**一个值得注意的细节**：启动路径的配置校验被注释掉了。[omni_proxy.sh:311-320](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/omni_proxy.sh#L311-L320)：

```bash
function start_nginx() {
    local nginx_conf_file="$1"
    # nginx -t -c "$nginx_conf_file"
    if [ $? -ne 0 ]; then
```

`nginx -t` 那行被注释，后面的 `$?` 检查实际检测的是上一条 `local` 赋值语句——永远为真，等于死代码。也就是说**普通启动不做配置校验**，配置错误只会在 nginx 真正加载时暴露；只有 `--reload` 路径会先 `nginx -t` 并在失败时自动回滚备份（[omni_proxy.sh:322-341](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/omni_proxy.sh#L322-L341)）。这正是本讲综合实践要你手动跑 `nginx -t` 的原因。

**环境变量兜底**（[omni_proxy.sh:292-300](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/omni_proxy.sh#L292-L300) + 生成配置中的 [omni_proxy.sh:476-479](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/omni_proxy.sh#L476-L479)）：`PYTHONHASHSEED`、`TORCH_DEVICE_BACKEND_AUTOLOAD` 在脚本侧给默认值，再经 nginx 的 `env` 指令白名单透传给 worker 进程——Nginx 默认会清空 worker 的环境变量，不声明 `env` 的话 C 模块内嵌的 Python 拿不到这两个变量。

**与手写示例的对照**：仓库里那份手写 [nginx.conf](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/nginx.conf) 就是生成结果的"人肉版"——它的 [L108-122](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/nginx.conf#L108-L122) server 块监听 7050、策略 sequential、encode 2 台 / prefill 4 台 / decode 16 台，可与生成物逐行互查。

#### 4.3.4 代码实践（可运行，需 bash 环境，不依赖 NPU 与 nginx）

1. **实践目标**：用 `--dry-run` 只生成配置、不启动 nginx，观察参数如何映射为指令。
2. **操作步骤**：

   ```bash
   # 示例代码：在本仓库 components/omni-proxy/omni_proxy/ 目录下执行
   cd components/omni-proxy/omni_proxy
   mkdir -p /tmp/op_tut

   bash omni_proxy.sh \
     --nginx-conf-file /tmp/op_tut/one_p_one_d.conf \
     --listen-port 7000 \
     --core-num 4 --start-core-index 0 \
     --prefill-endpoints 127.0.0.1:9000 \
     --decode-endpoints 127.0.0.1:9100,127.0.0.1:9101 \
     --omni-proxy-pd-policy sequential \
     --dry-run
   ```

   生成后检查：`grep -n "omni_proxy" /tmp/op_tut/one_p_one_d.conf` 与 `cat /tmp/op_tut/one_p_one_d.conf`。
3. **需要观察的现象**：
   - 脚本输出 `nginx.conf generated at /tmp/op_tut/one_p_one_d.conf` 与 `Dry run complete.`；
   - 配置中 `upstream prefill_endpoints` 只有 1 个 server，`upstream decode_endpoints` 有 2 个；
   - 没有提供 `--omni-proxy-model-path`，因此**不出现** `omni_proxy_model_path` 与 `omni_proxy_vllm_kv_port_offset` 指令；
   - 没有 `--omni-proxy-prefill-groups`，因此不出现分组指令。
4. **预期结果**：得到一份与本仓库 [nginx.conf](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/nginx.conf) 结构一致的配置。注意 `--dry-run` 仍会走到 `do_start` 的参数校验：漏掉 `--decode-endpoints` 会直接报 `Error: --decode-endpoints is required` 退出。本实践只涉及 bash 与文件写入，任何 Linux 机器可做；若本机 bash 版本过低导致 heredoc 异常，则标注"待本地验证"。

#### 4.3.5 小练习与答案

**练习 1**：`--listen-port` 默认值是 7150，为什么 u1-l4 部署的服务在 7000 端口？

**答案**：7150 只是脚本自身的默认值；生产部署中 ansible 的 proxy 配置把 `proxy_port`（7000）显式传给了 `--listen-port`。此外监听地址支持 `IP:PORT` 形式，未指定 IP 时默认 `0.0.0.0` 全接口监听（[README_CN.md:191-197](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/README_CN.md#L191-L197)）。

**练习 2**：`--reload` 与"改完配置重启 nginx"相比好在哪里？它靠什么保证不把坏配置加载进去？

**答案**：reload 走 Nginx 原生配置热加载（`nginx -s reload`），master 重新读配置、平滑拉起新 worker、排空旧 worker，已有连接不断。安全性来自 `do_reload` 的三步：重新生成配置 → `nginx -t` 校验，失败则从 `_bak` 备份回滚并中止 → 校验通过才 reload（[omni_proxy.sh:322-341](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/omni_proxy.sh#L322-L341)）。

**练习 3**：如果把 `--omni-proxy-prefill-groups "0:1,1:1"` 传了、`--omni-proxy-decode-groups` 忘了传，会发生什么？

**答案**：脚本本身不会报错——它只是把非空的分组参数渲染成指令（[omni_proxy.sh:460-470](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/omni_proxy.sh#L460-L470)），于是配置里只有 prefill 侧分组。但 README 明确约束两条指令必须同时配置或同时缺省，否则 **proxy 启动失败**（C 模块在解析配置时报错，[README_CN.md:147-149](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/README_CN.md#L147-L149)）；结合 4.3.3 讲过的"启动路径无 nginx -t"，这类错误要到 nginx 实际加载时才暴露。分组 id 不一一对应同样会导致启动失败。

## 5. 综合实践

**任务**：按讲义规格完成一条完整链路——用 `omni_proxy.sh` 生成一份 2 prefill + 2 decode 上游的 nginx.conf，再手改为 parallel 模式，用 `nginx -t` 验证两份配置的合法性。

**环境要求**：`nginx -t` 校验生成的配置时，nginx 要能成功 `load_module` 两个 `.so`（配置第 1-2 行写死了 `/usr/local/nginx/modules/` 路径）。因此本实践有两条路径：

- **路径 A（推荐）**：在 u1-l4 已部署环境的 C 节点容器内执行——nginx 与模块都已就位。`docker exec -it <C节点容器名> bash` 后操作，配置写到 `/tmp` 不影响线上服务。
- **路径 B**：在没有模块的机器上只能完成第 1-3 步（配置生成与 diff），第 4 步起 `nginx -t` 会报 `module ... is not found`（这是模块加载失败，不是语法错误），标注"待本地验证"。

**操作步骤**：

1. 生成 sequential 版配置（示例代码）：

   ```bash
   cd components/omni-proxy/omni_proxy
   bash omni_proxy.sh \
     --nginx-conf-file /tmp/op_tut/two_p_two_d.conf \
     --listen-port 7000 \
     --core-num 4 --start-core-index 0 \
     --prefill-endpoints 127.0.0.1:9000,127.0.0.1:9001 \
     --decode-endpoints 127.0.0.1:9100,127.0.0.1:9101 \
     --omni-proxy-pd-policy sequential \
     --dry-run
   ```

2. 校验 sequential 版（路径 A）：

   ```bash
   nginx -t -c /tmp/op_tut/two_p_two_d.conf
   ```

3. 手改为 parallel：编辑 `/tmp/op_tut/two_p_two_d.conf`，把 `location` 内的

   ```nginx
   omni_proxy_pd_policy sequential;
   ```

   改为

   ```nginx
   omni_proxy_pd_policy parallel;
   ```

   （该指令在生成配置中的位置对应 [omni_proxy.sh:536](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/omni_proxy.sh#L536) 渲染出的那一行。）

4. 再次校验：`nginx -t -c /tmp/op_tut/two_p_two_d.conf`。

5. 交叉验证：用 `--omni-proxy-pd-policy parallel` 直接再生成一份，与手改版 `diff`，应完全一致（证明手改与参数驱动两条路殊途同归）。

**需要观察的现象与预期结果**：

- 两次 `nginx -t` 均输出 `syntax is ok` 与 `test is successful`；
- 生成的配置里 `upstream prefill_endpoints` 与 `upstream decode_endpoints` 各有 2 个 `server` 行，均带 `max_fails=3 fail_timeout=10s`；
- diff 第 5 步两份文件无差异（唯一区别就是那一行策略值）；
- 语义提醒：parallel 模式要求上游引擎按"先预分配 D 侧 blocks、P 推流"的协议工作（SGLang 式协同）。本仓库默认 vLLM 部署使用 sequential；parallel 配置语法合法 ≠ 语义可用，切换模式需引擎侧配合。此结论来自 [README_CN.md:49-58](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/README_CN.md#L49-L58)，实际切换效果待本地验证。

## 6. 本讲小结

- omni-proxy 不重造 HTTP 服务器，而是以两个 **Nginx 动态模块**（`ngx_http_omni_proxy_module.so` + `ngx_http_set_request_id_module.so`）寄生在 Nginx 1.28.0 上，由 `build.sh --add-dynamic-module` 编译、`load_module` 加载，调度行为全部表达为 `omni_proxy_*` 配置指令。
- 调度的数据基础是**请求 10 阶段生命周期**（接收→Tokenize→APC matching→P 等待/已调度/执行→D 等待/已调度/执行→完成），每阶段埋点采集、共享内存同步，并直接映射为 access log 中的 `tknized/apc/wait_p/...` 字段。
- **sequential**（先 P 后 D，vLLM 式，本仓库默认）延迟选 D、KV 拉取串行暴露；**parallel**（P/D 同选，SGLang 式）D 预分配 blocks、KV 按层推送与 Prefill 重叠；脚本还藏有第三个策略 `aggregation`（不生成 prefill upstream）。
- `omni_proxy.sh` 是配置生成的单一事实源：约 40 个参数带默认值，经 heredoc 渲染成 nginx.conf，支持 dry-run/reload/rollback；分组调度用 `omni_proxy_prefill_groups`/`omni_proxy_decode_groups`，两者必须成对且 id 一一对应，否则 proxy 启动失败。
- 一个工程细节：普通启动路径的 `nginx -t` 校验被注释（死代码），只有 `--reload` 会先校验再热加载、失败自动回滚——所以手工验证配置必须自己跑 `nginx -t`。
- tokenizer 在 proxy 侧完成后随请求透传，下游引擎免重复分词（PD 分离场景省约 30% tokenizer 开销）；`PYTHONHASHSEED` 固定为 123 保证 proxy 与引擎的 block hash 一致，是 APC 匹配的前提。

## 7. 下一步学习建议

本讲建立了 omni-proxy 的架构观与配置面，下一讲进入它的内部实现：

- **u6-l2（调度器与 APC 缓存感知源码）**：精读 `omni_scheduler.c` 的请求排序与过载保护、`omni_apc.c`/`omni_radix_tree.c` 的全局前缀树、`omni_tokenizer.c` 的内嵌分词、`omni_zmq_handler.c` 的 KV 事件订阅——本讲所有"README 说了结论、没给实现"的点（排序权重公式、`omni_proxy_vllm_kv_port_offset 100` 的语义）都在那里落地。
- **u6-l3（在 ansible 体系里部署 proxy 与分组调度）**：看 `run_proxy` tag 如何拼装本讲的 `omni_proxy.sh` 命令、`USE_OMNI_PROXY` 与 `ENABLE_APC_EVENT` 的开启链路，以及 3P1D 下的分组配置实战。
- 建议同时回看 u4-l2/u4-l3：sequential 模式下 proxy 的"先 P 后 D"与 LLMDataDistConnector 的"D 主动拉取、P 延迟释放"、ZMQ 端口矩阵是同一枚硬币的两面——proxy 决定"谁传给谁"，connector 决定"怎么传"。
