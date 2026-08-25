# 实战：用 ansible 拉起第一个 1P1D BF16 服务

## 1. 本讲目标

学完本讲，你应该能够：

1. 独立完成部署前的四项准备：安装 ansible、配置 ssh 免密、拉取镜像、准备 BF16 权重。
2. 读懂 1P1D 的 inventory 与 BF16 模板之间的分工，知道哪些字段必改、哪些保持默认。
3. 说清 `run_docker`、`run_server`、`run_proxy` 三个 tag 各自执行哪些任务、按什么顺序执行。
4. 定位 `run_vllm_server_prefill_cmd` 与 `run_vllm_server_decode_cmd` 在模板中的位置，掌握「改脚本 = 改模板变量 + 重新跑 tag」的开关方式。
5. 在两台 A3 测试机上完整拉起 1P1D BF16 服务，并通过 `server_0.log` 跟踪启动阶段。

本讲是第一单元的落地环节：前几讲建立了「PD 分离是什么」「目录怎么组织」「inventory 字段怎么读」的认知，本讲把这些认知变成一次真实可执行的部署。

## 2. 前置知识

### 2.1 ansible 是什么

ansible 是一个「批量执行引擎」：你在一台**执行机**（控制机）上写好「对哪些机器、执行什么任务」，ansible 通过 ssh 依次登录目标机执行。三个核心概念：

| 概念 | 在本仓库的体现 | 通俗理解 |
|------|--------------|---------|
| inventory（清单） | `omni_infer_inventory_used_for_1P1D.yml` | 「机器花名册」：登记 P/D/C 三组机器的 IP 和属性 |
| playbook（剧本） | `omni_infer_server_template_performance1P1D_92B_bf16_open.yml` | 「施工图纸」：定义要对这些机器做的所有任务 |
| tag（标签） | `run_docker`、`run_server`、`run_proxy` 等 | 「施工开关」：用 `--tags` 只执行图纸中带该标签的任务 |

执行命令的形态是：

```bash
ansible-playbook -i <inventory> <playbook> --tags <标签>
```

### 2.2 docker 与 NPU 透传

推理服务跑在容器里。容器默认看不到宿主机的 NPU，必须把昇腾的设备文件（`/dev/davinci_manager`、`/dev/devmm_svm` 等）和驱动目录显式挂载/透传进去——这是本仓库 `docker run` 命令长长一串 `-v` 和 `--device` 的原因，第 4.2 节会逐项解释。

### 2.3 环境变量的三层传递

本部署链路里，一个配置值（比如 `MODEL_PATH`）要经过三跳才能到达 vLLM 进程：

1. **playbook 顶层 `environment:`** —— ansible 注入到每个目标机任务的 Shell 环境里；
2. **task 级 `environment:` + `docker exec -e`** —— 任务再把需要的变量塞进容器；
3. **容器内脚本的 `export`** —— `vllm_run_for_p.sh` 自己再导出运行时变量（HCCL、插件开关等）。

理解这条传递链，排查「为什么我的配置没生效」类问题时就不会迷路。

### 2.4 前置讲义回顾

本讲直接使用 [u1-l3](u1-l3-models-and-topologies.md) 已建立的事实：inventory 中 P/D/C 三组的字段含义、三级端口体系（proxy 7000 / node_port 8000 段 / api_port 9000 段）、模板文件名编码「拓扑+规格+精度+特性」。不再重复推导。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| [README.md](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README.md) | 部署总说明书 | 部署步骤的官方顺序：ssh → 镜像 → 改配置 → run_docker → run_server |
| [tools/ansible/92B/omni_infer_inventory_used_for_1P1D.yml](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_inventory_used_for_1P1D.yml) | 1P1D 节点清单 | 要改的 IP 字段 |
| [tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml) | 1P1D 92B BF16 服务模板 | 本讲主战场：environment、docker 命令、P/D/proxy 脚本、任务编排 |
| [tools/scripts/pd_run.sh](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/pd_run.sh) | 容器内最终启动脚本 | 本讲只讲它在链路中的位置，参数精读留给 u4-l4 |
| [tools/scripts/start_api_servers.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/start_api_servers.py) | 多 API server 拉起器 | `server_0.log` 等日志文件的来源 |

## 4. 核心概念与源码讲解

### 4.1 ansible-playbook：一条命令如何变成多机上的动作

#### 4.1.1 概念说明

playbook 把「部署」拆成一长串任务（task），每个任务带三个决定其是否执行的要素：

- **`when` 条件**：如 `'P' in group_names`，只对 P 组机器生效；
- **`tags` 标签**：如 `run_docker`，只有命令行 `--tags` 命中时才执行；
- **`hosts` 范围**：本模板 `hosts: all`，即 inventory 里 P、D、C 三组全部机器都会被扫到，靠 `when` 再细分。

所以模板是「一图多能」的：同一份文件，通过不同 tag 组合完成建容器、起服务、停服务、收日志等不同阶段的工作。

#### 4.1.2 核心流程

以本讲的两次执行为主线（1P1D、P 与 C 同机、D 单独一台机）：

```text
第一次执行 --tags run_docker
  ansible-playbook -i inventory playbook --tags run_docker
      ├─ (公共, tags=always) 生成实际容器名 ACTUAL_DOCKER_NAME_{P,D,C}
      ├─ (P/D 机) 检查并删除同名旧容器          [tags: run_docker, clean_up]
      ├─ (C 机)   检查并删除旧 proxy 容器        [tags: run_docker, clean_up]
      ├─ (P 机) docker run 创建 P 容器            [tags: run_docker]
      ├─ (D 机) docker run 创建 D 容器            [tags: run_docker]
      ├─ (C 机) docker run 创建 C 容器(多注入 PROXY_NODE_PORT) [tags: run_docker]
      └─ (三组机) 创建日志目录 LOG_PATH/<主机名>  [tags: run_docker]

第二次执行 --tags run_server,run_proxy
  ansible-playbook -i inventory playbook --tags run_server,run_proxy
      ├─ (公共, always) 汇总集群拓扑变量(PREFILL_API_SERVER_LIST 等)并 debug 打印
      ├─ (P 机) 探测默认网卡名 → 生成 vllm_run_for_p.sh
      ├─ (D 机) 生成 vllm_run_for_d.sh
      ├─ (D 机) docker exec 启动 decode 服务      ← 1P1D 先起 D
      ├─ (P 机) docker exec 启动 prefill 服务     ← 单机 P 后起, 走 mp 后端
      └─ (C 机) 杀旧 nginx → 生成并执行 run_proxy_server.sh 起 nginx+proxy
```

有两个容易踩坑的编排细节：

- **P 的启动任务有两份**，按 `NODE_IP_LIST` 里 IP 个数互斥执行：多机 P 实例（逗号分隔 ≥2 个 IP）先于 D 启动并使用 ray 后端；单机 P 实例（1 个 IP，1P1D 即此类）**排在 D 之后**启动，使用 mp 后端。
- **proxy 的三个任务同时挂着 `run_proxy` 和 `run_server` 两个 tag**，所以 README 里 `--tags run_server,run_proxy` 实际让 proxy 任务只执行一次（tag 是「或」命中，不会重复跑）。

#### 4.1.3 源码精读

**（1）play 头部：全机组执行、失败即全局终止**

[tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml:L3-L7](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L3-L7) 声明 `hosts: all`、`any_errors_fatal: true`、`max_fail_percentage: 0`——任何一台机器任务失败，整个 play 立即中止。这对 PD 分离是合理的：P、D 缺一不可，半启动的集群没有意义。`gather_facts: yes` 收集目标机事实，后续任务用 `ansible_env.XXX` 读取环境变量就依赖它。

**（2）inventory：本讲只改 IP**

[tools/ansible/92B/omni_infer_inventory_used_for_1P1D.yml:L3-L13](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_inventory_used_for_1P1D.yml#L3-L13) 的 `all.vars` 定义全局端口基址与登录方式：`ansible_user: root`、`global_port_base: 8000`、`base_api_port: 9000`、`proxy_port: 7000`，以及 `port_offset`（P 偏移 0、D 偏移 100）。

[tools/ansible/92B/omni_infer_inventory_used_for_1P1D.yml:L15-L43](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_inventory_used_for_1P1D.yml#L15-L43) 定义 p0/d0/c0 三台主机。部署时需要把占位 IP 换成真实 IP，README 明确要求 `ansible_host` 与 `host_ip` **两个字段都要改**：

> [README.md:L57](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README.md#L57)「注意 `ansible_host` 和 `host_ip` 都要修改为部署的 IP 地址」，且 **proxy 节点（C 组）设为 P 节点 IP**。

按 1P1D 的端口公式（承接 u1-l3），假设 P 机 IP 为 `192.168.1.10`、D 机为 `192.168.1.11`，改完后各节点端口为：

| 节点 | ansible_host / host_ip | node_port | api_port |
|------|----------------------|-----------|----------|
| p0 | 192.168.1.10 | 8000 + 0 + 0 = **8000** | 9000 + 0 + 0 = **9000** |
| d0 | 192.168.1.11 | 8000 + 100 = **8100** | 9000 + 100 + 0 = **9100** |
| c0 | 192.168.1.10（与 P 同机） | 7000 + 0 = **7000** | — |

**（3）容器名生成：`always` tag 的第一个任务**

[tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml:L436-L442](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L436-L442) 用 `set_fact` 把 environment 里的容器名与 inventory 主机名拼接成实际容器名：

```text
ACTUAL_DOCKER_NAME_P = "docker_p" + "_" + "p0" → docker_p_p0
ACTUAL_DOCKER_NAME_D = "docker_d" + "_" + "d0" → docker_d_d0
ACTUAL_DOCKER_NAME_C = "docker_c" + "_" + "c0" → docker_c_c0
```

`tags: always` 保证无论跑哪个 tag，后续任务都能引用这三个事实变量。加主机名后缀是为了多机实例场景下同名容器不冲突。

**（4）run_docker 阶段：先删旧、再建新**

- [L444-L474](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L444-L474)：P/D 组机器先用 `docker inspect` 查同名容器，存在则 `docker stop` + `docker rm -f`（`failed_when: false` 让「容器不存在」不算错误）；
- [L476-L505](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L476-L505)：对 C 组做同样清理；
- [L507-L528](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L507-L528)：三个 `Run container` 任务分别对 P/D/C 组执行 `docker run`（命令内容见 4.2 节）；
- [L530-L535](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L530-L535)：在每台机器创建 `LOG_PATH/<inventory_hostname>` 日志目录。

这就是 README 提示「重复运行会覆盖同名容器」的机制来源：run_docker 天然是幂等重入的。

**（5）run_server 阶段：拓扑汇总与互斥的 P 启动**

- [L582-L687](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L582-L687)：`Register all values` 用 Jinja2 遍历 inventory，算出 `PREFILL_API_SERVER_LIST`（P 侧 API 地址表）、`DECODE_API_SERVER_LIST_ALL`（形如 `ip:9100@15`，`@15` 表示从 9100 起连续 16 个端口）、`PREFILL_POD_NUM`/`DECODE_POD_NUM`（按 `host_ip` 去重计数）等集群级变量，`delegate_to: localhost` 表示在执行机本地算、再广播给所有主机；
- [L712-L728](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L712-L728)：`Display all values` 把这些汇总值打印出来——**这是部署前排错的第一现场**，1P1D 应能看到 `PREFILL_POD_NUM: 1`、`NODE_IP_LIST` 只含 P 机一个 IP；
- [L832-L852](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L832-L852)：多机 P 实例的启动任务，条件 `NODE_IP_LIST` 拆分后 ≥2 个 IP，先起 P 再等 20 秒（[L854-L859](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L854-L859)）；
- [L861-L881](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L861-L881)：D 组启动任务；
- [L884-L906](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L884-L906)：单机 P 实例的启动任务，条件 `NODE_IP_LIST` 恰好 1 个 IP——**1P1D 走的是这一条**，执行顺序在 D 之后。

**（6）run_proxy 阶段**

[L990-L1034](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L990-L1034) 依次：生成杀 nginx 脚本并执行（清掉旧 proxy）→ 生成 `run_proxy_server.sh` → `docker exec` 在 C 容器里执行它。三个任务都带 `run_proxy` 与 `run_server` 双标签。

**（7）模板里还有哪些 tag**

除本讲三个主角外，模板还内置：`clean_up`（删容器）、`stop_server`（[L730-L787](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L730-L787)，杀容器内 python/ray 进程）、`sync_code`（[L537-L580](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L537-L580)，同步并拷贝 omni-npu 代码进容器）、`reload_proxy`（热重载 proxy 配置）、`fetch_log`（[L1091-L1107](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L1091-L1107)，把各机日志拉回执行机）、`delete_node`（缩容删节点）与 `proc_bind`（CPU 绑核，默认 `proc_bind_enabled` 未定义即为 false，相关任务自动跳过）。本讲只需记住前三个。

#### 4.1.4 代码实践：不改任何机器，先做一次「干跑读图」

1. **实践目标**：在不触碰集群的前提下，验证你对 tag 与任务归属的理解。
2. **操作步骤**：
   - 在执行机上（无需 NPU）执行：
     ```bash
     ansible-playbook -i omni_infer_inventory_used_for_1P1D.yml \
       omni_infer_server_template_performance1P1D_92B_bf16_open.yml \
       --tags run_docker --list-tasks
     ```
   - 再把 `--tags` 换成 `run_server,run_proxy` 与 `stop_server` 各执行一次 `--list-tasks`。
3. **需要观察的现象**：三次输出的任务列表差异；`--list-tasks` 只解析不执行，不会连接目标机。
4. **预期结果**：
   - `run_docker` 列表包含「Check and delete…」「Run container for …」三个建容器任务与日志目录任务；
   - `run_server,run_proxy` 列表包含 Register/Display、生成 vllm_run_for_p/d.sh、两处 prefill 启动任务、decode 启动任务、nginx 相关三个任务；
   - `stop_server` 列表只有杀进程与生成 kill 脚本的任务。
   - 具体任务名以你的 `--list-tasks` 输出为准（待本地验证）。
5. 若暂无 ansible 环境，可用 `yum install ansible` 安装后仅做本练习，不产生任何集群变更。

#### 4.1.5 小练习与答案

**练习 1**：为什么 README 要求 `--tags run_server,run_proxy` 而不是只写 `run_server`？

**答案**：proxy 的三个任务同时带 `run_proxy` 和 `run_server` 标签，只写 `run_server` 在这份模板里其实也能带起 proxy；但显式写出两个 tag 让意图清晰，且当模板调整（proxy 任务摘掉 `run_server` 标签）时命令仍然正确。反过来，若只想重启 proxy 而不动 P/D 服务，可单独用 `--tags run_proxy`（甚至 `reload_proxy` 热重载），这就是 tag 机制提供的运维粒度。

**练习 2**：任务「Run the Omniai service for prefill instances」在模板里出现两次，ansible 如何知道执行哪一份？

**答案**：靠互斥的 `when` 条件。第一份条件是 `NODE_IP_LIST` 按逗号拆分后长度 ≥2（多机 P 实例，ray 后端），第二份是长度 ==1（单机 P，mp 后端）。1P1D 的 P 组只有 p0，`NODE_IP_LIST` 只有一个 IP，因此只执行第二份，且顺序在 decode 之后。

**练习 3**：如果 `--tags run_server` 跑到一半 D 机断网，会发生什么？

**答案**：play 头部 `any_errors_fatal: true` 且 `max_fail_percentage: 0`，任一主机任务失败立刻终止整个 play，不会留下「半启动」继续跑的假象；P/proxy 若尚未启动则不会再启动，需要排除故障后重跑 `run_server,run_proxy`。

### 4.2 docker 容器管理：NPU 设备如何进入容器

#### 4.2.1 概念说明

昇腾 NPU 不像 GPU 那样被 docker 原生枚举，容器要用上 NPU 必须拿到三类东西：

1. **设备文件**：`/dev/davinci_manager`（设备管理）、`/dev/devmm_svm`（虚拟内存管理）、`/dev/hisi_hdc`（主机-设备拷贝通道）；
2. **驱动与工具**：`/usr/local/Ascend/driver`、`/usr/local/bin/npu-smi`、`/usr/local/sbin` 等；
3. **网络与内存**：`--net=host` 共享宿主机网络（PD 节点间通信、无数端口映射的关键）、`--shm-size` 给共享内存足够的配额。

模板把这些全部固化在 `docker_run_cmd` 变量里，P、D、C 三个容器复用同一条基础命令，只各自追加差异项。

#### 4.2.2 核心流程

```text
start_docker_cmd_p = docker_run_cmd + "-d --name $DOCKER_NAME_P $DOCKER_IMAGE_ID"
start_docker_cmd_d = docker_run_cmd + "-d --name $DOCKER_NAME_D $DOCKER_IMAGE_ID"
start_docker_cmd_c = docker_run_cmd + "-e PROXY_NODE_PORT=$NODE_PORT
                                   + -d --name $DOCKER_NAME_C $DOCKER_IMAGE_ID"
```

`docker_run_cmd` 的关键参数一览：

| 参数 | 取值 | 作用 |
|------|------|------|
| `--shm-size=500g` | 500 GB | 多进程共享内存上限，PD 传输与 HCCL 依赖大共享内存 |
| `--net=host` | — | 容器直接用宿主机网络栈，8000/9000/7000 端口无需映射 |
| `--privileged=true` | — | 特权模式，便于容器内访问设备与执行运维操作 |
| `--device=/dev/davinci_manager` 等 | 3 个设备 | NPU 设备文件透传 |
| `-v /usr/local/Ascend/driver:…` 等 | 多个挂载 | 驱动、DCMI、npu-smi、hccn 工具对齐宿主机版本 |
| `-v $LOG_PATH:$LOG_PATH` 等 | 路径等值挂载 | 日志、权重、脚本在容器内外路径一致，便于宿主机直接 tail |
| `--entrypoint=bash` | — | 容器起来只开 bash，服务由后续 `docker exec` 按阶段启动 |

#### 4.2.3 源码精读

**（1）基础 docker 命令**

[tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml:L31-L57](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L31-L57) 定义 `docker_run_cmd`，覆盖上表全部参数。注意 L54-L56 三个等值挂载 `$LOG_PATH:$LOG_PATH`、`$MODEL_PATH:$MODEL_PATH`、`$SCRIPTS_PATH:$SCRIPTS_PATH`——容器内路径与宿主机完全一致，这让后续 `docker exec` 与宿主机 `tail -f` 看到的是同一份文件。

**（2）三个容器的差异项**

[tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml:L354-L365](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L354-L365)：

- `start_docker_cmd_p`/`start_docker_cmd_d`：只追加 `-d --name <容器名> <镜像>`，后台常驻；
- `start_docker_cmd_c`：额外注入 `-e PROXY_NODE_PORT=$NODE_PORT`，把 C 节点的 `node_port`（= 7000）作为环境变量带进容器，proxy 启动脚本据此监听。

**（3）run_docker 任务如何使用它们**

[L507-L528](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L507-L528) 三个任务分别 `command: bash -c "{{ start_docker_cmd_x }}"`，并用 task 级 `environment` 把 `ACTUAL_DOCKER_NAME_*` 传给 Shell 展开为实际容器名；C 组任务同时传入 `NODE_PORT: "{{ node_port }}"`。结合 4.1 节：P 机上最终建成 `docker_p_p0`，D 机上建成 `docker_d_d0`，C 机（与 P 同机）上建成 `docker_c_c0`。

**（4）镜像从哪来**

README 给出 A3 的拉取命令：[README.md:L14-L20](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README.md#L14-L20)

```bash
docker pull swr.cn-east-4.myhuaweicloud.com/omni-ci/omniinfer-a3-arm:release_1.2.1.post1-202607241407-vllm
```

拉取后把完整「名字:标签」填进模板的 `DOCKER_IMAGE_ID`（见 4.3 节）。

#### 4.2.4 代码实践：验证容器建好了、NPU 看得见

1. **实践目标**：确认 `run_docker` 之后三个容器存在，且容器内能识别 NPU。
2. **操作步骤**（在 P 机与 D 机上）：
   ```bash
   docker ps --format '{{.Names}} {{.Image}} {{.Status}}'
   docker exec docker_p_p0 npu-smi info        # 容器内查看 NPU
   docker inspect docker_p_p0 --format '{{.HostConfig.ShmSize}}'
   ```
3. **需要观察的现象**：
   - `docker ps` 能看到 `docker_p_p0`（P 机）/ `docker_d_d0`（D 机）/ `docker_c_c0`（C 机，与 P 同机时两台容器并存）；
   - `npu-smi info` 在容器内列出 16 张 Ascend 910C 卡；
   - ShmSize 输出约 536870912000（500 GB 对应的字节数）。
4. **预期结果**：三项都符合即容器层就绪。若 `npu-smi info` 报找不到设备，优先排查宿主机驱动与 `--device` 挂载是否被改动（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 C 容器要在 `docker run` 时额外传 `PROXY_NODE_PORT`，而 P/D 容器不需要？

**答案**：C 容器的 node_port（7000）来自 inventory 的 `proxy_port + node_rank`，proxy 启动时需要知道监听端口；P/D 的端口类变量（`MASTER_PORT`、`API_PORT`）不是在建容器时注入，而是在 `run_server` 阶段由 `docker exec -e` 传入（见 4.3 节），因为那时集群拓扑变量（如 api_port）才完成汇总。

**练习 2**：把 `--net=host` 去掉会发生什么？

**答案**：容器进入独立网络命名空间，P/D/C 之间按 IP 直连的 8000/9000/7000 端口全部不可达，且模板里没有任何 `-p` 端口映射，PD 通信与客户端请求都会失败。`--net=host` 是这套「无映射、按端口段规划」方案的前提。

**练习 3**：镜像升级后如何重建容器？

**答案**：改 `DOCKER_IMAGE_ID` 后重跑 `--tags run_docker` 即可——该 tag 的任务自带「检查同名容器 → stop → rm -f」的前置清理，天然覆盖旧容器；README 也提醒镜像未变时无需重复执行此命令。

### 4.3 环境变量配置：从 playbook 到 vLLM 进程的传递链

#### 4.3.1 概念说明

模板顶层的 `environment:` 是部署的「控制面板」：ansible 会把这些键值对注入**每个任务在目标机上执行时**的 Shell 环境。它承担两类配置：

- **必改项**：路径与镜像、容器名——跟你的机器强相关；
- **默认项**：模型名、KV 连接器、并行度——除非换规格，一般不动。

由于 `gather_facts: yes`，这些变量在任务里可通过 `ansible_env.LOG_PATH` 这样的形式取回。而真正进入 vLLM 进程的环境变量，还要再经过 task 级 `environment` → `docker exec -e` → 脚本内 `export` 两跳。

#### 4.3.2 核心流程

```text
play environment（全局, 必改项在此）
   │  ansible 注入到每个任务的 Shell
   ▼
task environment（按任务补充拓扑变量: NODE_PORT/API_PORT/SERVER_IP_LIST...）
   │  任务 Shell 中展开
   ▼
docker exec -e A=$A -e B=$B ...（把选定的变量带入容器, 见 docker_start_vllm_cmd_p/d）
   │
   ▼
容器内 vllm_run_for_p.sh / vllm_run_for_d.sh（脚本内再 export HCCL_*/VLLM_PLUGINS 等）
   │
   ▼
pd_run.sh → start_api_servers.py → vllm 进程
```

#### 4.3.3 源码精读

**（1）顶层 environment：必改项清单**

[tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml:L9-L28](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L9-L28)（与 README 的说明段 [README.md:L78-L98](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README.md#L78-L98) 对照）：

| 变量 | 示例值 | 必改？ | 说明 |
|------|--------|-------|------|
| `LOG_PATH` | `/path/to/server/log/` | **必改** | 日志根目录，须提前存在且全程有效，否则无法跟踪服务 |
| `MODEL_PATH` | `/path/to/model/weights/` | **必改** | 本机 BF16 权重路径，P、D 所有节点保持一致 |
| `MODEL_LEN_MAX_PREFILL/DECODE` | `524288` | 默认 | P/D 两侧最大模型长度 |
| `LOG_PATH_IN_EXECUTOR` | `/path/to/server/log_path_in_executor` | 可选 | `fetch_log` 时把远端日志汇总到执行机 |
| `CODE_PATH` | `/path/to/code/` | 可选 | `sync_code` 同步 omni-npu 代码时使用 |
| `KV_CONNECTOR` | `LLMDataDistConnector` | 默认 | KV 传输连接器（u4 展开） |
| `SERVED_MODEL_NAME` | `openPangu-2.0-Flash` | 默认 | 对外服务名，**请求体 model 字段必须与它一致** |
| `DOCKER_IMAGE_ID` | `image_name:image_tag` | **必改** | 与各机 `docker pull` 到的镜像完全一致 |
| `DOCKER_NAME_P/D/C` | `docker_p` 等 | **必改** | 容器名前缀，实际容器名会追加 `_主机名` |
| `SCRIPTS_PATH` | `/tmp/scripts_path` | 默认 | 生成的脚本（vllm_run_for_p.sh 等）存放处 |
| `DECODE_TENSOR_PARALLEL_SIZE` | `1` | 默认 | 脚本默认 prefill TP 部署、decode DP 部署 |

**（2）task 级 environment：给 docker exec 喂变量**

[tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml:L368-L389](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L368-L389) 定义 `docker_start_vllm_cmd_p`：一串 `-e MODEL_PATH=$MODEL_PATH -e PREFILL_SERVER_LIST=$PREFILL_SERVER_LIST …` 后 `docker exec -d $DOCKER_NAME_P /bin/bash -c $SCRIPTS_PATH/vllm_run_for_p.sh`。其中 `$XXX` 的值来自调用该命令的任务（[L884-L906](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L884-L906)）的 task 级 environment，例如 `API_PORT: "{{ api_port }}"`（inventory 算出 9000）、`SERVER_IP_LIST`（D 机 IP 表）、`PREFILL_TENSOR_PARALLEL_SIZE`（= `ascend_rt_visible_devices` 里卡数 = 16）、`SOCKET_IFNAME`（下一任务探测的默认网卡名）。

D 侧对应 [L391-L411](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L391-L411) 的 `docker_start_vllm_cmd_d`，多传 `NUM_SERVERS`（16 个 DP API server）、`HOST`（inventory 主机名，供 `server_offset` 查表）等；调用它的任务是 [L861-L881](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L861-L881)。

**（3）网卡名探测**

[tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml:L817-L830](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L817-L830) 用 `ip -4 route list 0/0 | awk '{print $5}' | head -1` 取默认路由网卡名存为 `default_interface`，随后作为 `SOCKET_IFNAME` 传入 P/D 启动命令，最终成为 `pd_run.sh` 的 `--gloo-socket-ifname/--tp-socket-ifname`。这就是 README 故障排查段 `HCCL_SOCKET_IFNAME` 的「自动版」——脚本默认帮你选了默认路由网卡，只有多网卡机器选错时才需要手工覆盖。

**（4）容器内脚本自己的 export**

P 侧脚本变量 [L62-L100](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L62-L100) 里有一段「# patch」注释开头的环境变量（`VLLM_PLUGINS`、`OMNI_NPU_VLLM_PATCHES`、`CUSTOM_MODEL_CONFIG_PATH` 等），它们把 u2 将要讲的 omni-npu 插件与补丁机制打开。本讲只需知道：**改这些开关 = 改模板里这一段 + 重跑 `--tags run_server,run_proxy`**，不需要重建容器。

#### 4.3.4 代码实践：必改项自检清单

1. **实践目标**：在跑 `run_docker` 之前，用静态检查发现漏改项。
2. **操作步骤**：
   - 打开模板，逐项核对 4.3.3 表格中标「必改」的 6 个值（LOG_PATH、MODEL_PATH、DOCKER_IMAGE_ID、DOCKER_NAME_P/D/C）已不是 `/path/to/...` 或 `image_name:image_tag` 占位符；
   - 在两台机器上确认路径真实存在：
     ```bash
     ls /your/log/path /your/model/weights/Config.json  # 按你的实际路径
     docker images | grep omniinfer-a3-arm
     ```
3. **需要观察的现象**：权重目录下能看到模型配置文件；`docker images` 能列出与 `DOCKER_IMAGE_ID` 完全一致的条目。
4. **预期结果**：全部命中即可进入部署；任何一项是占位符或路径不存在，`run_docker`/`run_server` 会在挂载或启动处报错。`CODE_PATH` 只在使用 `sync_code` 时才必须配置。

#### 4.3.5 小练习与答案

**练习 1**：`SERVED_MODEL_NAME` 改成 `my-model` 后，发请求要注意什么？

**答案**：请求体 JSON 的 `model` 字段必须填 `my-model`，与 `SERVED_MODEL_NAME` 完全一致（README [L156](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README.md#L156) 有明确提示），否则请求会被拒。默认 92B 填 `openPangu-2.0-Flash`。

**练习 2**：为什么 P、D 节点的 `MODEL_PATH` 必须一致？

**答案**：P 与 D 各自加载同一份模型权重（P 做 prefill、D 做 decode），路径在模板里是全局变量，两侧容器挂载同一占位路径；若某台机器路径不同，需在该机改为本机实际路径——但指向的必须是同一规格同一精度的权重，否则 KV 传输与 logits 语义不匹配。

**练习 3**：`environment` 里的变量与 task 级 `environment` 同名时，谁生效？

**答案**：task 级覆盖 play 级。例如 `run_docker` 的「Run container」任务里 `DOCKER_NAME_P: "{{ ACTUAL_DOCKER_NAME_P }}"` 覆盖了 play 级的 `DOCKER_NAME_P: docker_p`，使命令实际使用拼接后的容器名。理解这一点，读任务时就不会被两处同名变量迷惑。

### 4.4 prefill/decode 启动脚本：位置、开关与日志

#### 4.4.1 概念说明

P、D 的服务不是 ansible 直接拉起的，而是三步走：

1. ansible 把模板里的多行字符串变量「渲染 + 落盘」为容器外路径 `$SCRIPTS_PATH/vllm_run_for_p.sh`（或 `_d.sh`）；
2. `docker exec -d` 在容器内后台执行该脚本；
3. 脚本调用 `/workspace/omniinfer/tools/scripts/pd_run.sh`，由它拼装参数并最终通过 `start_api_servers.py` 拉起一个或多个 vLLM API server。

因此**「开关一个特性」的完整动作是：改模板 `vars` 里的脚本变量 → 重跑 `--tags run_server,run_proxy`**（脚本会重新生成并覆盖，旧进程先由 stop_server 或脚本自身逻辑处理）。

#### 4.4.2 核心流程

```text
模板 vars: run_vllm_server_prefill_cmd (L62-L148)
模板 vars: run_vllm_server_decode_cmd  (L150-L244)
        │ copy content=... dest=$SCRIPTS_PATH/vllm_run_for_p.sh|_d.sh  (L797-L815)
        ▼
docker exec -d <P容器> /bin/bash -c $SCRIPTS_PATH/vllm_run_for_p.sh    (单机P: L884-L906)
docker exec -d <D容器> /bin/bash -c $SCRIPTS_PATH/vllm_run_for_d.sh    (L861-L881)
        │ 容器内 cd /workspace/omniinfer/tools/scripts && bash pd_run.sh ...
        ▼
pd_run.sh common_operations → python start_api_servers.py --log-dir $LOG_PATH/<主机名>
        ▼
LOG_PATH/<主机名>/ 下产生:
  run_prefill.log / run_decode.log   ← 脚本自身的 stdout/stderr 重定向
  server_0.log ... server_15.log     ← 每个 API server rank 一个日志文件
```

日志文件的命名来源可从源码直接确认：[tools/scripts/start_api_servers.py:L211](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/start_api_servers.py#L211) 按 rank 打开 `server_{rank}.log`；而 pd_run.sh 在 [tools/scripts/pd_run.sh:L395-L401](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/pd_run.sh#L395-L401) 的 `common_operations` 里调用它并透传 `--log-dir`。

P/D 脚本的关键差异（决定两侧行为的开关大多在这里）：

| 维度 | prefill 脚本 | decode 脚本 |
|------|-------------|-------------|
| `--role` / `--kv-role` | `prefill` / `kv_producer`（[L131-L132](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L131-L132)） | `decode` / `kv_consumer`（[L228-L229](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L228-L229)） |
| 并行度 | `--tp ${PREFILL_TENSOR_PARALLEL_SIZE}`（16 卡 = TP16 单实例） | `--tp ${DECODE_TENSOR_PARALLEL_SIZE}`（=1，配合 `--num-servers 16` 成 16 个 DP server） |
| 执行后端 | 单机 mp / 多机 ray（[L104-L117](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L104-L117)） | 固定 `--distributed-executor-backend mp` |
| kv_rank | `${KV_RANK}`（inventory 的 kv_rank，1P1D 为 0） | `${PREFILL_POD_NUM}`（P 实例数，1P1D 为 1） |
| 图编译 | `--enforce-eager`（prefill 关图模式） | `--compilation-config … cudagraph_mode FULL`（decode 开全图，见 u5-l2） |
| HCCL 缓冲 | `HCCL_BUFFSIZE=100` | `HCCL_BUFFSIZE=1200` |
| 自定义模型配置 | `..._xp1d_p_open.json`（[L83](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L83)） | `..._xp1d_d_open.json`（[L172](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L172)） |

两侧共同点：`KV_PARALLEL_SIZE=$((PREFILL_POD_NUM + 1))`，即 1P1D 下 \( KV\_PARALLEL\_SIZE = 1 + 1 = 2 \)（P 占 rank 0，D 占 rank 1）——这正是 u4-l1 的主题，本讲记住结论即可。

#### 4.4.3 源码精读

**（1）脚本生成任务**

[L797-L815](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L797-L815) 两个 `copy` 任务分别把 `run_vllm_server_prefill_cmd` / `run_vllm_server_decode_cmd` 写成 `vllm_run_for_p.sh` / `vllm_run_for_d.sh`（mode 0750）。注意 decode 任务带 `vars: server_offset_dict`，用于脚本内 Jinja2 展开成 `declare -A config_dict`（多 D 组时的 server 偏移表）。

**（2）P 脚本调用 pd_run.sh**

[tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml:L120-L148](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L120-L148)：`cd /workspace/omniinfer/tools/scripts` 后以 `--role "prefill"` 调用 pd_run.sh，末尾把全部输出重定向到 `${LOG_PATH}/{{ inventory_hostname }}/run_prefill.log` 并 `&` 后台化。`{{ inventory_hostname }}` 是 Jinja2 占位，生成脚本时会被替换为 p0。

**（3）D 脚本调用 pd_run.sh**

[tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml:L214-L244](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L214-L244)：同样结构，`--role "decode"`、`--kv-role "kv_consumer"`，额外传 `--num-servers ${NUM_SERVERS}`、`--num-dp ${dp}`、`--server-offset ${config_dict[$HOST]:-0}`；日志重定向到 `run_decode.log`。

**（4）proxy 脚本**

[L246-L291](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L246-L291) 的 `run_proxy_cmd` 把 Jinja2 渲染出的 `{{ PREFILL_API_SERVER_LIST }}`、`{{ DECODE_API_SERVER_LIST_ALL }}` 展开（后者形如 `ip:9100@15`，脚本内循环把 `@15` 展开成 16 个连续端口），最后 `bash omni_proxy.sh --listen-port $PROXY_NODE_PORT … --omni-proxy-pd-policy sequential` 启动 nginx+proxy。u6 会精读 omni_proxy.sh，本讲只需知道 proxy 监听 7000、按「先 prefill 后 decode」的 sequential 策略转发。

**（5）模板自带的「就绪探测」示例**

[L908-L932](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L908-L932) 演示了判断服务就绪的正规姿势：循环 `docker exec … grep -q "Application startup complete" …/server_0.log`，最多等 300 秒。该任务默认只在 `proc_bind_enabled` 为 true 时启用，但 grep 的目标字符串本身适用于任何部署——你在综合实践中可以用同样的关键字判断 D 侧就绪。

#### 4.4.4 代码实践：开关一个特性并观察日志变化

1. **实践目标**：体验「改模板变量 → 重跑 tag → 看日志」的完整闭环，并以 `--max-num-seqs` 为例。
2. **操作步骤**：
   - 在模板 P 脚本的 `EXTRA_ARGS`（[L92](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L92)）中把 `--max-num-seqs 4` 改为 `--max-num-seqs 2`；
   - 重跑 `--tags run_server,run_proxy`；
   - 在 P 机上对比重新生成的脚本与日志：
     ```bash
     grep max-num-seqs /tmp/scripts_path/vllm_run_for_p.sh
     tail -f /your/log/path/p0/run_prefill.log
     ```
3. **需要观察的现象**：落盘脚本中参数已更新；`run_prefill.log` 里 pd_run.sh 拼装的 `vllm serve` 命令行出现 `--max-num-seqs 2`。
4. **预期结果**：参数变化通过日志中的最终命令行得到确认。若日志无变化，检查是否改在 D 脚本（`run_decode.log` 对应 decode 侧 `--max-num-seqs 3`）或忘记重跑 tag（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：`vllm_run_for_p.sh` 生成在容器里还是宿主机上？为什么容器能执行它？

**答案**：生成在宿主机的 `$SCRIPTS_PATH`（默认 `/tmp/scripts_path`）。`docker_run_cmd` 里有 `-v $SCRIPTS_PATH:$SCRIPTS_PATH` 与 `-v /tmp:/tmp` 的等值挂载，容器内同路径可见；`docker exec -d <容器> /bin/bash -c $SCRIPTS_PATH/vllm_run_for_p.sh` 因此能直接执行。

**练习 2**：想在 D 侧关掉 `--enable-lopt`（并行 tokenizer），改哪里？要不要重建容器？

**答案**：改模板 decode 变量里 `EXTRA_ARGS` 字符串（[L202](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L202)）删去 `--enable-lopt --lopt-pool-size 16 --lopt-chunk-size 4096`，重跑 `--tags run_server,run_proxy` 即可。不需要重建容器，也不需要重跑 `run_docker`——特性开关全部在服务层。

**练习 3**：`server_0.log` 与 `run_prefill.log` 内容有何区别？

**答案**：`run_prefill.log`/`run_decode.log` 是脚本自身的输出重定向（pd_run.sh 的打印、报错栈最先出现在这里）；`server_N.log` 是 `start_api_servers.py` 为第 N 个 API server 单独打开的日志（[start_api_servers.py:L211](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/start_api_servers.py#L211)），vLLM 引擎的启动里程碑（如 `Application startup complete`）在后者。排障顺序一般是先 `run_*.log` 看脚本层，再 `server_*.log` 看引擎层。

## 5. 综合实践

**任务：在两台 A3 测试机上完整拉起 1P1D BF16 服务，并记录 `server_0.log` 的关键启动阶段。**

前提：两台 16 卡 A3 机器（P 机兼 C 节点、D 机各一），BF16 权重已就位，执行机（可与 P 机同机）已装 ansible。完整流程如下，逐步执行并记录：

1. **执行机安装 ansible**：`yum install ansible`（或对应发行版等价命令）。
2. **配置 ssh 免密**（在 P 机执行，参照 [README.md:L22-L34](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README.md#L22-L34)）：
   ```bash
   ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519
   ssh-copy-id -i ~/.ssh/id_ed25519.pub root@<D机IP>
   ssh-copy-id -i ~/.ssh/id_ed25519.pub root@<P机IP>
   ```
   验证：从 P 机 `ssh root@<D机IP> hostname` 不再要求密码。
3. **两台机器拉取镜像**（[README.md:L14-L20](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README.md#L14-L20)），并 `docker images` 记下完整 tag。
4. **改 inventory**（[tools/ansible/92B/omni_infer_inventory_used_for_1P1D.yml:L15-L43](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_inventory_used_for_1P1D.yml#L15-L43)）：p0 与 c0 填 P 机 IP，d0 填 D 机 IP；每台 `ansible_host` 与 `host_ip` 都要改。
5. **改模板 environment**（[L9-L28](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L9-L28)）：按 4.3.3 的必改清单填 `LOG_PATH`、`MODEL_PATH`、`DOCKER_IMAGE_ID` 与三个容器名。
6. **建容器**（在模板所在目录执行）：
   ```bash
   ansible-playbook -i omni_infer_inventory_used_for_1P1D.yml \
     omni_infer_server_template_performance1P1D_92B_bf16_open.yml --tags run_docker
   ```
   结束后按 4.2.4 的实践验证 `docker ps` 与容器内 `npu-smi info`。
7. **起服务与 proxy**：
   ```bash
   ansible-playbook -i omni_infer_inventory_used_for_1P1D.yml \
     omni_infer_server_template_performance1P1D_92B_bf16_open.yml --tags run_server,run_proxy
   ```
   执行过程中记录 `Display all values` 任务打印的 `PREFILL_POD_NUM`（应为 1）、`DECODE_API_SERVER_LIST_ALL`（应形如 `<D机IP>:9100@15`）。
8. **跟踪日志**（D 机上，路径换成你的 LOG_PATH）：
   ```bash
   tail -f /your/log/path/d0/server_0.log
   ```
   按出现顺序记录以下关键阶段（每个记一行时间戳）：模型权重开始加载 → 权重加载完成 → KV transfer/connector 初始化相关日志 → `Application startup complete` → Uvicorn/API server 监听 9100。P 机同样观察 `p0/server_0.log`（TP16 单 server）与 `p0/run_prefill.log`。
9. **发请求验证**（参照 [README.md:L152-L176](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README.md#L152-L176)，`model` 填 `openPangu-2.0-Flash`，端口 7000 打到 P 机 IP），收到回答即部署成功。
10. **若多机通信失败**：按 [README.md:L146-L150](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README.md#L146-L150) 在 P/D 脚本中加 `export HCCL_SOCKET_IFNAME=<ifconfig 查到的网卡名>` 后重跑 `run_server`。

预期结果：第 8 步日志阶段齐全、第 9 步返回模型回答。受限于环境（是否真有两台 A3），本实践标注：**具体日志行待本地验证**，若某阶段缺失，回到对应 `run_*.log` 排查脚本层错误。排障细节将在 [u1-l5](u1-l5-request-and-troubleshoot.md) 展开。

## 6. 本讲小结

- ansible 部署的心智模型：inventory 管「有哪些机器」，playbook 管「做什么事」，`--tags` 管「这次做哪几段」；本模板用 `when: 'P'/'D'/'C' in group_names` 让同一份任务列表在三类节点上各取所需。
- 三个核心 tag 的分工：`run_docker` 建容器（自带旧容器清理，幂等可重入）、`run_server` 生成并执行 P/D 服务脚本、`run_proxy` 起 nginx+proxy；proxy 任务同时挂前两个 tag，所以官方命令是 `--tags run_server,run_proxy`。
- NPU 容器的三要素：`--device` 透传昇腾设备文件、`-v` 挂载驱动与工具、`--net=host` 让 8000/9000/7000 端口段直接可达；P/D/C 容器共用 `docker_run_cmd`，C 只多一个 `PROXY_NODE_PORT`。
- 环境变量三层传递：play `environment`（必改项：LOG_PATH/MODEL_PATH/DOCKER_IMAGE_ID/容器名）→ task `environment` + `docker exec -e` → 容器脚本内 `export`（HCCL、插件开关）。
- `run_vllm_server_prefill_cmd`/`run_vllm_server_decode_cmd` 是 P/D 行为的总开关，位于模板 `vars` 段（L62-L148 / L150-L244）；改完重跑 `run_server,run_proxy` 即生效，无需重建容器。
- 日志三层定位法：`run_prefill.log`/`run_decode.log` 看脚本层，`server_N.log`（`start_api_servers.py` 按 rank 生成）看引擎层，`Application startup complete` 是就绪标志。

## 7. 下一步学习建议

- 下一讲 [u1-l5：请求测试、日志跟踪与常见问题排查](u1-l5-request-and-troubleshoot.md)将把本讲拉起的服务当靶场：对比流式/非流式请求、跟踪日志、复现并修复 HCCL 多机通信问题。
- 服务跑通后，建议回看模板 P/D 脚本中 `--kv-transfer-config`、`--kv-rank`、`--kv-parallel-size` 相关参数（本讲只给出了 `KV_PARALLEL_SIZE = PREFILL_POD_NUM + 1` 的结论），为 [u4-l1：PD 分离与 KV 传输配置全景](u4-l1-pd-kv-transfer-config.md)做准备。
- 对容器内 `omni-npu` 如何被 vLLM 加载好奇的读者，可以预习 [u2-l1：vLLM 插件体系与 omni-npu 的三个入口](u2-l1-vllm-plugin-entry.md)，本讲 P/D 脚本里的 `VLLM_PLUGINS`、`OMNI_NPU_VLLM_PATCHES` 环境变量正是那一切的开关。
