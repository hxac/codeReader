# 生产综合实战：505B 全特性部署方案设计

## 1. 本讲目标

本讲是整个学习手册的收官实战。前面九个单元分别把 ansible 部署（u1）、omni-npu 插件（u2/u3）、PD 分离 KV 链路（u4）、性能机制（u5）、omni-proxy 调度（u6）、omni-cache 主机内存池（u7）、jointfix 量化（u8）、omni-eplb（u9）各自讲透了。本讲把它们**组装到同一套生产拓扑上**。

读完本讲你应当能够：

1. 拿到一份空白的机器清单，独立规划出 openPangu-2.0-Pro（505B）在 **4P81D16** 形态下的节点分组、kv_rank、node_rank 与全部端口。
2. 说出 INT8 量化、OmniCache、omni-proxy 分组调度三项特性**各自改了模板里的哪些行**，以及它们叠加时互相影响的参数（最典型的是 KV_PARALLEL_SIZE 的计算规则会被 OmniCache 改写）。
3. 输出一份可复用的上线检查清单：从大页内存预留、容器重建、服务就绪判据，到发压验证与回退步骤。

本讲的立场是「方案设计」而非「跑通一次」：所有结论都从仓库真实文件推导，凡是需要真机才能确认的数字，一律标注「待本地验证」。

## 2. 前置知识

本讲默认你已完成 u8-l4、u7-l3、u6-l3，并把以下结论当作已知事实。这里只做最简回顾，细节请回看对应讲义。

**PD 分离的三类节点与三级端口（u1-l3/u1-l4）**
- inventory 用 `P` / `D` / `C` 三组分配角色：P 做 prefill（计算密集），D 做 decode（访存密集），C 跑 nginx + omni-proxy 作统一入口。
- 端口三级体系：`proxy_port`（默认 7000，客户端入口）→ `node_port`（8000 段，P 按 `kv_rank×10` 划块、D 固定 8100）→ `api_port`（9000 段，proxy 转发目标）。

**KV 传输配置（u4-l1/u4-l2）**
- `kv-transfer-config` 是四字段 JSON：`kv_connector`、`kv_role`、`kv_rank`、`kv_parallel_size`。
- 基线规则：`PREFILL_POD_NUM` = P 组不同 `host_ip` 的数量；`KV_PARALLEL_SIZE = PREFILL_POD_NUM + 1`；D 侧 `kv_rank` 固定取 `PREFILL_POD_NUM`。**本讲会看到这条规则在 OmniCache 分支下被改写**。

**OmniCache（u7-l1/u7-l2/u7-l3）**
- KV 的流动路径变成「P 侧 HBM → P 主机内存池（hugetlbfs 大页）→ OX 传输引擎 → D 主机内存池 → D HBM」。
- 三层默认值：代码 `os.getenv` 兜底 < 文档默认 < ansible 模板显式值；排障以模板 if 分支的 `export` 与运行日志回显为准。
- 切回普通部署三步：重启容器 → `set_hugepage_limit.sh --target-pages 262144` → 用 `free` 与 `/proc/meminfo` 的 `HugePages_` 计数验证。

**INT8 / W8A8（u8-l1～u8-l4）**
- W8A8 = 权重 8bit per-channel 静态量化 + 激活 8bit per-token 动态量化，由 jointfix 产出。
- 量化信息**装在权重目录的 `config.json` 的 `quantization_config` 里**（compressed-tensors 标准），vLLM 自动识别，所以部署命令里没有任何 `--quantization` 参数，`--dtype bfloat16` 保留（计算精度仍是 bfloat16）。
- w8a8 模板与 BF16 模板的差异集中在「用显存富余兑换批量 / 并发 / KV Cache 精度」的少数几行。

**omni-proxy 分组调度（u6-l1/u6-l2/u6-l3）**
- 分组由 `omni_proxy_prefill_groups` / `omni_proxy_decode_groups` 两条指令表达，段式语法「组id：数量」按 server 顺序消费。
- 三条启动校验：成对配置、两侧去重后的 id 集合相等、**段数必须等于该侧 upstream server 数**。运行期 decode 请求必须落在与其 prefill 同 group 的节点上。
- 未配置分组时全员落在 group 0。
- APC（前缀缓存感知）是双端链路：proxy 侧 `--omni-proxy-model-path` + 引擎侧 `--kv-events-config`，缺一端 radix tree 匹配恒空。

**一个术语提醒**：本讲说的「实例（pod / instance）」指一个逻辑推理引擎，可能跨多台机器；「机器」指物理主机。inventory 里用 `host_ip` 标识实例主节点、`node_rank` 标识实例内机器编号，这正是「一实例多机」的表达方式（u1-l3）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README.md) | 顶层部署手册：BF16 主线流程、ssh 免密、必改项、请求测试 |
| [README_INT8.md](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README_INT8.md) | INT8 部署手册，含 4P81D16 形态说明与 OmniCache 切换前的大页释放步骤 |
| [tools/ansible/505B/omni_infer_inventory_used_for_4P81D16.yml](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_inventory_used_for_4P81D16.yml) | 本讲主拓扑：4 个单机 P 实例 + 1 个双机 D 实例 + 1 个 C 节点 |
| [tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml) | 本讲主模板：INT8 权重 + OmniCache + omni-proxy 全特性叠加 |
| [tools/ansible/505B/omni_infer_server_template_performance2P1D_505B_bf16_open.yml](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance2P1D_505B_bf16_open.yml) | 对照组：505B BF16 基线模板，用于 diff 出「特性带来了哪些行变化」 |
| [tools/ansible/505B/omni_infer_inventory_used_for_2P1D.yml](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_inventory_used_for_2P1D.yml) | 对照拓扑：2 个双机 P 实例 + 1 个四机 D 实例 |
| [components/omni-proxy/omni_proxy/omni_proxy.sh](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/omni_proxy.sh) | proxy 配置生成器：分组参数 CLI 与 nginx 指令渲染、dry-run |
| [components/omni-proxy/omni_proxy/modules/ngx_http_omni_proxy_module.c](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/ngx_http_omni_proxy_module.c) | 分组三条启动校验的 C 实现 |
| [components/omni-cache/tools/setup/set_hugepage_limit.sh](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-cache/tools/setup/set_hugepage_limit.sh) | 宿主机大页预留总量的设置与恢复脚本 |
| `components/omni-npu/src/omni_npu/model_config/configs/low_latency/openpangu_v2/pangu_v2_moe_bf16_a3_505B_xp1d_{p,d}_omnicache_claw.json` | OmniCache 形态下 P/D 各自的模型最佳实践配置（与 `*_claw.json` 基线对照） |

## 4. 核心概念与源码讲解

### 4.1 大规模拓扑规划：把 4P81D16 拆成一张账

#### 4.1.1 概念说明

「4P81D16」是 505B 在 OmniCache 形态下的推荐部署形态。README_INT8 对它有一句权威解释：

> openPangu-2.0-Pro: `omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml` 推荐 inventory 形态: 4P81D16 (4个单机组P实例, 1个双机组D实例)

见 [README_INT8.md:190-192](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README_INT8.md#L190-L192)。拆开读就是：

- **4 个单机组 P 实例**：`P0`/`P1`/`P2`/`P3` 各含一台机器，每台 16 卡，`kv_rank` 分别为 0/1/2/3，prefill 走 TP16。
- **1 个双机组 D 实例**：`D0` 组含 `d0`、`d1` 两台机器，两台的 `host_ip` 都指向实例主节点 `d0`，`node_rank` 为 0/1。decode 侧每台机器 16 卡按 TP1×DP16 拆成 16 个独立 server，两机合计 **32 个 decode server**。
- **1 个 C 节点**：跑 nginx + omni-proxy，监听 7000。

合计 6 台 A3 机器，与 README_INT8 规格表里的「4P81D16（6机A3）」一致（[README_INT8.md:9-12](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README_INT8.md#L9-L12)）。

命名里 `81`/`16` 这几个数字的官方拆法仓库未给出进一步说明，语义以上面那句中文解释为准（**待确认**，不要望文生义）。

#### 4.1.2 核心流程

规划一套拓扑就是依次回答五个问题：

1. **有几个 P 实例？** 决定 `PREFILL_POD_NUM`（= P 组不同 `host_ip` 数）与 KV 并行规模。
2. **每个 P 实例几台机器？** 单机走 `mp` 后端；多机（`NODE_IP_LIST` 含逗号）走 Ray，且 `PREFILL_TENSOR_PARALLEL_SIZE` 会乘上机器数。
3. **D 实例怎么拆？** `DECODE_TENSOR_PARALLEL_SIZE=1` + 每机 16 卡 → DP；跨机 D 靠 `server_offset` 修正全局 DP 编号。
4. **端口怎么分？** 套公式：`node_port(P) = 8000 + kv_rank×10`，`api_port(P) = 9000 + kv_rank×10 + node_rank`，D 侧 `node_port = 8100`、`api_port = 9100 + node_rank`。
5. **C 节点放哪？** 一般复用某台 P 机的 IP，`ansible_host` 与 `host_ip` 都填它。

#### 4.1.3 源码精读

**端口偏移的全局定义**在 inventory 头部：

[omni_infer_inventory_used_for_4P81D16.yml:8-13](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_inventory_used_for_4P81D16.yml#L8-L13) 定义了 `global_port_base: 8000`、`base_api_port: 9000`、`proxy_port: 7000`，以及 `port_offset` 里 P=0（8000-8099 段）、D=100（8100-8199 段）。这三级端口体系与 u1-l3 讲的完全一致，只是这里由模板统一换算。

**P 组的分组结构**：

[omni_infer_inventory_used_for_4P81D16.yml:16-27](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_inventory_used_for_4P81D16.yml#L16-L27) 是 `P0` 的写法：`children: P: children: P0/P1/...`，每个 `Pn` 组一台主机，`kv_rank` 唯一，`node_rank` 恒 0，`ascend_rt_visible_devices` 列满 16 卡。`P1`/`P2`/`P3` 结构相同，仅 `kv_rank` 递增（[L28-L57](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_inventory_used_for_4P81D16.yml#L28-L57)）。README 也专门提示过：多 P 节点必须用这种分组结构并逐个设置 `kv_rank`（[README.md:73](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README.md#L73)）。

**D 组的「一实例双机」写法**：

[omni_infer_inventory_used_for_4P81D16.yml:59-76](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_inventory_used_for_4P81D16.yml#L59-L76) 里 `d0`（node_rank=0）与 `d1`（node_rank=1）共用 `host_ip: "127.0.0.5"`。这个「多机同 host_ip」就是「同一实例」的记号：`PREFILL_POD_NUM` 式的去重统计会把它算成 1 个 D 实例，而 Ray 组网、`server_offset` 都以它为单位。

**C 节点**：

[omni_infer_inventory_used_for_4P81D16.yml:78-85](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_inventory_used_for_4P81D16.yml#L78-L85) 把 C 放在 `127.0.0.1`（即复用 P0 机器），`node_port = proxy_port + node_rank = 7000`。

**模板如何把 inventory 翻译成规模数字**（这是规划表的「计算器」）：

- [omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml:789-796](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml#L789-L796) 用 `groups['P'] | map(attribute='host_ip') | unique | length` 算出 `PREFILL_POD_NUM`，对 4P81D16 结果是 **4**。
- [omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml:816-817](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml#L816-L817) 把所有 D 主机的 `ascend_rt_visible_devices` 拼成 `DECODE_SERVER_ALL` 并累加出 `DECODE_SERVER_OFFSET` 字典——两台 D 机各 16 卡，拼串后 31 个逗号，decode 脚本里 `dp = 逗号数 + 1 = 32`；offset 字典为 `{"d0": 0, "d1": 16}`。
- [omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml:757-788](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml#L757-L788) 生成 proxy 的上游清单：`PREFILL_API_SERVER_LIST` 收集 `ansible_host == host_ip` 的 P 主机（4 条），`DECODE_API_SERVER_LIST` 把每台 D 机压成 `ip:api_port@卡数减一` 的紧凑格式（2 条）。
- [omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml:364-381](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml#L364-L381) 在 proxy 脚本里把 `@15` 展开成 16 个连续端口，最终得到 **4 个 prefill 上游 + 32 个 decode 上游**。

**多机 P 实例的启动顺序**：模板对 P 节点写了两个互斥任务——`NODE_IP_LIST` 含 2 台以上的先起（[L1009-L1035](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml#L1009-L1035)，走 Ray），单机的后起（[L1080-L1106](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml#L1080-L1106)，中间隔 20 秒，[L1037-L1042](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml#L1037-L1042)）。4P81D16 的 P 全是单机实例，因此实际走的是后一条路径（4 台并行、无 Ray）。

把以上推导整理成一张规划总账（模板里的 `127.0.0.x` 是占位 IP，落地时全部换成真实地址）：

| 主机 | 组 | node_rank | kv_rank | node_port | api_port | host_ip | 角色 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| p0 | P0 | 0 | 0 | 8000 | 9000 | p0 自身 | prefill TP16，kv_producer |
| p1 | P1 | 0 | 1 | 8010 | 9010 | p1 自身 | prefill TP16，kv_producer |
| p2 | P2 | 0 | 2 | 8020 | 9020 | p2 自身 | prefill TP16，kv_producer |
| p3 | P3 | 0 | 3 | 8030 | 9030 | p3 自身 | prefill TP16，kv_producer |
| d0 | D0 | 0 | — | 8100 | 9100 | d0 自身（实例主） | decode，16 个 DP server，offset 0 |
| d1 | D0 | 1 | — | 8100 | 9101 | **d0**（不是 d1 自己） | decode，16 个 DP server，offset 16 |
| c0 | C | 0 | — | 7000 | — | c0 | nginx + omni-proxy |

由此派生的规模数字：`PREFILL_POD_NUM = 4`；decode `dp = 32`；KV 事件端口（P 侧 `api_port + 100`）= 9100 / 9110 / 9120 / 9130；proxy 上游 = 4 prefill + 32 decode。

#### 4.1.4 代码实践

**实践目标**：不碰真机，用只读命令验证你对拓扑与模板的理解，并亲手算出规划总账。

**操作步骤**：

1. 在仓库根目录做语法检查（不连任何主机）：
   ```bash
   ansible-playbook -i tools/ansible/505B/omni_infer_inventory_used_for_4P81D16.yml \
     tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml --syntax-check
   ```
2. 列出模板的全部任务与 tag，确认 `run_docker` / `run_server` / `run_proxy` / `stop_server` 的边界：
   ```bash
   ansible-playbook -i tools/ansible/505B/omni_infer_inventory_used_for_4P81D16.yml \
     tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml --list-tasks
   ```
3. 手工填写上面那张规划总账（不要看答案），重点是 `d1` 的 `host_ip` 必须填 d0。
4. 用一行 shell 验证 offset 推导：把两台 D 机的 `ascend_rt_visible_devices`（各 16 个元素）用逗号拼接，数逗号：
   ```bash
   echo -n "0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15" | tr -cd ',' | wc -c
   ```
   输出 31，加 1 即 `dp = 32`——这正是模板 [L244-L245](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml#L244-L245) 的算法。

**需要观察的现象**：`--syntax-check` 输出 `playbook ... had no syntax errors`；`--list-tasks` 能看到任务名和 tag 列表；手算表格与 4.1.3 的总账逐格一致。

**预期结果**：端口、rank、上游数量全部对上，特别是 `DECODE_SERVER_OFFSET` 为 `{"d0": 0, "d1": 16}`。真实 ansible 执行结果**待本地验证**（需要 6 台 A3 与已拉取镜像）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 4P81D16 的 `d1` 的 `host_ip` 误写成 `d1` 自己的 IP，会发生什么？

**答案**：`host_ip` 不再与 `ansible_host` 去重合并，D 会被统计成 2 个实例而非 1 个双机实例：`DECODE_SERVER_IP_LIST` 的排序逻辑（`host_ip == ansible_host` 的插到队首，否则追加，[L806-L815](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml#L806-L815)）会把两台机器都当主节点，Ray 组网与 `server_offset` 的全局编号都会错位，decode server 的 DP rank 互相重叠。

**练习 2**：对比 [omni_infer_inventory_used_for_2P1D.yml:16-53](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_inventory_used_for_2P1D.yml#L16-L53) 与 4P81D16 的 P 组，两者「多机」的方向有什么不同？

**答案**：2P1D 是 **2 个双机 P 实例**（`P0` 含 `p0`/`p1` 两台机器、`kv_rank=0`，`P1` 含 `p2`/`p3`、`kv_rank=1`），prefill 需要走 Ray 跨机组网，TP 会乘上机器数；4P81D16 是 **4 个单机 P 实例**，prefill 各自 `mp` 后端独立成引擎，靠 `kv_rank` 区分。前者扩大单实例算力，后者扩大 prefill 吞吐与 KV 生产者数量。

**练习 3**：`DECODE_POD_NUM` 在两份 505B 模板里的计算方式一样吗？

**答案**：不一样，这是个跨模板复用配置时的坑。4P1D OmniCache 模板用 `groups['D'] | length`（[L805](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml#L805)，对 4P81D16 得 2，按机器数），而 2P1D BF16 模板用 `unique | length`（[omni_infer_server_template_performance2P1D_505B_bf16_open.yml:635-642](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance2P1D_505B_bf16_open.yml#L635-L642)，按实例数）。同一个 4P81D16 inventory 喂给两份模板会得到 2 和 1 两个值。该变量在本模板的 proxy 命令里并未使用，但复用模板前必须逐个核对语义。

### 4.2 特性组合：INT8 + OmniCache + 分组调度如何叠进同一份模板

#### 4.2.1 概念说明

「组合特性」的本质是：**一份 ansible 模板 = 一个基线骨架 + 若干特性的 if 分支与参数差异**。本讲的 4P1D 模板文件名 `performance4P1D_505B_int8_open_omni_cache.yml` 已经把四要素编码进去了（拓扑 4P1D + 规格 505B + 精度 int8 + 特性 omni_cache，u1-l3 的命名规则）。

三项特性各自「住在」模板的不同层面：

| 特性 | 住在哪 | 开关形式 |
| --- | --- | --- |
| INT8 (W8A8) | 权重目录的 `config.json`（compressed-tensors） | `MODEL_PATH` 指向 jointfix 产物即可，模板无量化参数 |
| OmniCache | `environment` + `run_vllm_server_{prefill,decode}_cmd` 的 if 分支 | `ENABLE_OMNI_CACHE`，并改写 `KV_CONNECTOR` / `KV_PARALLEL_SIZE` |
| 分组调度 | `run_proxy_cmd` 里的 `omni_proxy.sh` 参数 | `--omni-proxy-prefill-groups` / `--omni-proxy-decode-groups`（**当前模板未配置，需自行添加**） |

#### 4.2.2 核心流程

三项特性叠加后的装配流程：

```text
ansible-playbook --tags run_docker
  └─ 用 docker_run_cmd 建 P/D/C 容器（OmniCache 模板 --shm-size=1600g、--privileged）

ansible-playbook --tags run_server,run_proxy
  ├─ P 侧脚本 vllm_run_for_p.sh
  │    ├─ ENABLE_OMNI_CACHE=1 分支：
  │    │    export 一组 OMNI_CACHE_* / MAP_SIZE_BYTES / BASE_PORT / ZMQ_BASE_PORT
  │    │    bash setup_hugetlbfs_2MB.sh          ← 建主机内存池
  │    │    register_connectors()                ← 注册 OmniCacheConnector
  │    │    KV_CONNECTOR="OmniCacheConnector"；KV_PARALLEL_SIZE=1     ← 改写基线规则！
  │    └─ pd_run.sh ... --kv-connector ${KV_CONNECTOR} --kv-parallel-size ${KV_PARALLEL_SIZE}
  ├─ D 侧脚本 vllm_run_for_d.sh
  │    ├─ 同样建池 + 注册，但 MAP_SIZE/LAYER_BYTES 与 P 不同
  │    └─ KV_PARALLEL_SIZE=$((dp + 1)) = 33      ← 又一处改写！
  └─ C 侧脚本 run_proxy_server.sh
       ├─ USE_OMNI_PROXY=1 → omni_proxy.sh --omni-proxy-model-path ...（APC 的 proxy 端）
       └─ （读者需补）--omni-proxy-prefill-groups / --omni-proxy-decode-groups
```

**关键认知：`KV_PARALLEL_SIZE` 的计算规则会被 OmniCache 改写。** 基线（u4-l1）是 `PREFILL_POD_NUM + 1 = 5`；打开 OmniCache 后 P 侧变成 1、D 侧变成 `dp + 1 = 33`。这不是矛盾，而是 connector 换了：`OmniCacheConnector` 的组网以主机内存池为中心，P 侧各自独立成池（无需在 kv 维度组网），D 侧要把 32 个 DP server 加 1 当作 KV 并行规模。**排障时看到 `KV_TRANSFER_CONFIG` 回显是 1/33 而不是 5/5，不要当成配置错误。**

#### 4.2.3 源码精读

**(a) OmniCache 的开关与环境变量面**

[omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml:19-31](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml#L19-L31) 在 play 级 `environment` 里集中声明了 OmniCache 的参数化入口：`ENABLE_OMNI_CACHE`（默认 1）、`ENABLE_HOST_MAPPING`（默认 0）、两个内存池文件名 `OMNI_CACHE_{PREFILL,DECODE}_MMAP_FILE`、`DISABLE_GATHER_SELECTION`、DSA 分池开关 `ENABLE_OMNI_CACHE_DSA_SPLIT`、`HYBRID_ATTN_GROUP_SIZE`（默认 18）、以及 OX 引擎的 `BASE_PORT: 16077` 与 `ZMQ_BASE_PORT: 16555`。注意这些全部带 `| default(...)` 兜底——这正是 u7-l3 讲的「三层默认值」里的模板显式层，优先级最高。同时 [L43](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml#L43) 的 `USE_OMNI_PROXY: "1"` 选定了 proxy 侧走 omni-proxy 而非 global_proxy。

**(b) P 侧的 OmniCache 分支**

[omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml:145-173](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml#L145-L173) 是 P 侧完整分支，按行读：

- [L152-L153](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml#L152-L153)：`OMNI_CACHE_LAYER_BYTES=88046829568`（注释 82GB，换算 88046829568 / 2³⁰ = 82 GiB，正确）；`MAP_SIZE_BYTES=1610612736000`（= 1500 GiB）。
- [L163-L164](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml#L163-L164)：`KV_CACHE_MEMORY_BYTES = OMNI_CACHE_LAYER_BYTES × HYBRID_ATTN_GROUP_SIZE` = 88046829568 × 18 = 1,584,842,932,224 字节 ≈ **1476 GiB**，再以 `--kv-cache-memory-bytes` 显式告诉 vLLM KV 预算。
- [L166-L167](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml#L166-L167)：调用 `setup_hugetlbfs_2MB.sh` 在容器内挂 hugetlbfs 建池（u7-l3 讲过：特权容器内的改动即宿主机内核改动）。
- [L169-L172](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml#L169-L172)：注册 connector，然后 **`KV_CONNECTOR="OmniCacheConnector"`、`KV_PARALLEL_SIZE=1`**——覆盖了 play 级 `KV_CONNECTOR: LLMDataDistConnector`（[L17](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml#L17)）和脚本前部算好的 `PREFILL_POD_NUM + 1`（[L110](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml#L110)）。
- else 分支 [L174-L178](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml#L174-L178)：`ENABLE_OMNI_CACHE=0` 时回落 `LLMDataDistConnector`，回到 u4 的基线链路。

**(c) D 侧的 OmniCache 分支**

[omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml:272-313](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml#L272-L313) 结构对称但有四处 D 侧特有差异：

- [L277-L278](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml#L277-L278)：`OMNI_CACHE_LAYER_BYTES=27917287424`、`MAP_SIZE_BYTES=549755813888`。**注意这两行注释写 48GB / 1000GB，但按字节数换算分别是 26 GiB 与 512 GiB——注释与数值不符，以字节数为准**（这是 u10-l1 就确立的「README/注释与源码不一致时以源码为准」原则的又一例）。
- [L289](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml#L289)：`OMNI_CACHE_LOCAL_DP_SIZE=16`，D 侧本地 DP 规模。
- [L296-L303](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml#L296-L303)：`ENABLE_OMNI_CACHE_DSA_SPLIT=1` 时为 DSA indexer 的 KV 单独再建一个池（大小取主池的 80%，页数换算成 2MiB 页向上取整后追加），对应 u3-l2 讲的「kv_cache[1] 供 Indexer」那段缓存。
- [L307-L308](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml#L307-L308)：`KV_CONNECTOR="OmniCacheConnector"`、**`KV_PARALLEL_SIZE=$((dp + 1)) = 33`**。而 `--kv-rank` 仍传 `${PREFILL_POD_NUM}` 即 4（[L339](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml#L339)）。

**(d) INT8 在模板里的「隐身」**

模板里没有任何量化参数：`--dtype bfloat16` 依旧（[L113](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml#L113) P 侧、[L257](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml#L257) D 侧），量化完全由 `MODEL_PATH` 指向的权重目录携带（u8-l4 的 compressed-tensors 装箱标准）。连模型最佳实践配置都叫 `..._bf16_...`：[L107](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml#L107) P 侧与 [L236](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml#L236) D 侧分别钉死 `low_latency/openpangu_v2/pangu_v2_moe_bf16_a3_505B_xp1d_{p,d}_omnicache_claw.json`——文件名里的 `bf16` 指的是算子策略基线，不是权重精度（u5-l1 讲过该 json 只装 ModelParallelConfig 与 ModelOperatorOptConfig 两层，`CUSTOM_MODEL_CONFIG_PATH` 是最高优先级覆盖通道）。

**(e) 特性组合还改了「模型最佳实践配置」本身**

对照基线 `*_claw.json` 与 OmniCache 版 `*_omnicache_claw.json`，P 侧多出/翻转 4 个键：`enable_mome_sp: true`（仅 OmniCache 版有）、`use_rope_fusion_op: false→true`、`disable_npu_top_k_top_p_sample: true→false`（即打开融合采样器）、`optimize_first_chunk: false→true`，见 [pangu_v2_moe_bf16_a3_505B_xp1d_p_omnicache_claw.json](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/model_config/configs/low_latency/openpangu_v2/pangu_v2_moe_bf16_a3_505B_xp1d_p_omnicache_claw.json)；D 侧翻转 2 个键（`use_rope_fusion_op`、`disable_npu_top_k_top_p_sample`），见 [pangu_v2_moe_bf16_a3_505B_xp1d_d_omnicache_claw.json:12-18](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/model_config/configs/low_latency/openpangu_v2/pangu_v2_moe_bf16_a3_505B_xp1d_d_omnicache_claw.json#L12-L18)。也就是说：**特性组合不只发生在环境变量层，还会换掉整份算子策略 json**。这也是 u5-l1 强调「生产模板一律显式钉死配置文件」的原因。

**(f) APC 双端接线（这份模板是完整示例）**

- 引擎端：[L111-L112](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml#L111-L112) 把 `ENDPOINT_PORT` 设为 `api_port + 100`，拼出 `KV_EVENTS_CONFIG`，并在 [L113](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml#L113) 以 `--kv-events-config` 传入——P 侧在 9100/9110/9120/9130 发 ZMQ KV 事件。
- proxy 端：[L397](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml#L397) 传 `--omni-proxy-model-path $MODEL_PATH`，让 proxy 内嵌 tokenizer 并启用 APC 匹配。

u6-l3 说过 92B 基线模板两端都没接线，**这份 505B OmniCache 模板才是 APC 的完整参考实现**。

**(g) 分组调度：模板没配，要自己加**

[omni_proxy.sh:190-197](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/omni_proxy.sh#L190-L197) 定义了两个 CLI 参数（帮助文本见 [L75-L76](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/omni_proxy.sh#L75-L76)），[L460-L470](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/omni_proxy.sh#L460-L470) 把逗号分隔的段渲染成 nginx 指令 `omni_proxy_prefill_groups 0:1 1:1;`。

C 侧三条校验的实现在 [ngx_http_omni_proxy_module.c:3404-3428](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/ngx_http_omni_proxy_module.c#L3404-L3428)：只配一侧报 EMERG（L3408-L3418），两侧去重后的组 id 集合必须逐元素相等（L3424-L3428，具体比较在 [L3387-L3399](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/ngx_http_omni_proxy_module.c#L3387-L3399)）；[L3456-L3469](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/ngx_http_omni_proxy_module.c#L3456-L3469) 再校验**段数分别等于 prefill / decode 的 upstream server 数**。

对本拓扑（4 prefill 上游 + 32 decode 上游），唯一同时满足三条校验的 4 组方案是：

```bash
--omni-proxy-prefill-groups "0:1,1:1,2:1,3:1"   # 4 段 = 4 个 server，id 集 {0,1,2,3}
--omni-proxy-decode-groups   "0:8,1:8,2:8,3:8"   # 4 段 = 32 个 server，id 集 {0,1,2,3}
```

如果写成 decode `"0:32"`（1 段、id 集 {0}），会同时撞上「id 集合不相等」和「段数 1 ≠ server 数 32」两条 EMERG。当前模板 [L383-L401](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml#L383-L401) 的 `omni_proxy.sh` 调用里没有这两个参数，因此默认全员 group 0（不分组也能跑，只是 4 个 P 实例之间没有配对隔离）。

**(h) 与 BF16 基线模板的行级 diff（「特性带来了什么」的定量答案）**

对照 [omni_infer_server_template_performance2P1D_505B_bf16_open.yml](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance2P1D_505B_bf16_open.yml)，值得抄进方案的差异：

| 维度 | 2P1D BF16 | 4P1D INT8+OmniCache | 出处 |
| --- | --- | --- | --- |
| `--shm-size` | 500g | **1600g** | [bf16:L33](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance2P1D_505B_bf16_open.yml#L33) vs [cache:L50](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml#L50) |
| `PYTHONHASHSEED` | 1234 | **123**（APC 匹配前提） | [bf16:L35](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance2P1D_505B_bf16_open.yml#L35) vs [cache:L52](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml#L52) |
| P 侧 `--max-num-batched-tokens` | 4096 | **49152** | [bf16:L89](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance2P1D_505B_bf16_open.yml#L89) vs [cache:L113](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml#L113) |
| P 侧 `--enable-prefix-caching` | 由 `ENABLE_PREFIX_CACHING` 开关 | **显式开启** | [bf16:L94-L100](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance2P1D_505B_bf16_open.yml#L94-L100) vs [cache:L113](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml#L113) |
| D 侧前缀缓存 | 未显式关闭 | **`--no-enable-prefix-caching`**（OmniCache 下 D 侧强制关） | [cache:L257](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml#L257) |
| D 侧图捕获档位 | `cudagraph_capture_sizes:[12]` | **`[16]`** | [bf16:L200](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance2P1D_505B_bf16_open.yml#L200) vs [cache:L257](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml#L257) |
| D 侧 `--max-num-seqs` | 3 | **4** | 同上两行 |
| LOPT | `--enable-lopt --lopt-pool-size 16 --lopt-chunk-size 4096` | **未启用**，且 `OMNI_SKIP_DECODE_TOKENIZE=0` | [bf16:L91/L169-L170](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance2P1D_505B_bf16_open.yml#L169-L170) vs [cache:L224-L225](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml#L224-L225) |
| P 侧 `HCCL_BUFFSIZE` / `GPU_UTIL` | 100 / 0.9 | **300 / 0.95** | [bf16:L67/L103](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance2P1D_505B_bf16_open.yml#L67) vs [cache:L89/L116](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml#L89) |
| proxy 参数 | `--prefill-pod-size/--decode-pod-size`、无 model-path | **`--omni-proxy-model-path`、`--omni-proxy-tokenize-chunk-bytes 16384`、`--omni-proxy-max-batch-num-token 300000`** | [bf16:L294-L299](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance2P1D_505B_bf16_open.yml#L294-L299) vs [cache:L397-L401](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml#L397-L401) |

其中「为何 OmniCache 模板关掉了 LOPT 与 `OMNI_SKIP_DECODE_TOKENIZE`」仓库内没有文档说明（可能与 proxy 侧 tokenize 接管后的分工有关），**待确认**——方案文档里应把它列为已知差异而不是猜测原因。

#### 4.2.4 代码实践

**实践目标**：在不碰 NPU 的前提下，为 4P81D16 生成一份带分组调度的合法 nginx 配置，并用源码里的三条校验自检。

**操作步骤**：

1. 在装好 omni-proxy 组件（或已拷贝 `omni_proxy/` 目录）的环境里，用 dry-run 渲染配置（`--dry-run` 只生成 nginx.conf 不启动 nginx，见 [omni_proxy.sh:84](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/omni_proxy.sh#L84) 与 [L258-L259](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/omni_proxy.sh#L258-L259)）：
   ```bash
   cd components/omni-proxy/omni_proxy/
   bash omni_proxy.sh --dry-run \
     --listen-port 7000 \
     --prefill-endpoints "10.0.0.1:9000,10.0.0.2:9010,10.0.0.3:9020,10.0.0.4:9030" \
     --decode-endpoints "10.0.0.5:9100,10.0.0.5:9101,...,10.0.0.6:9116" \
     --omni-proxy-prefill-groups "0:1,1:1,2:1,3:1" \
     --omni-proxy-decode-groups   "0:8,1:8,2:8,3:8"
   ```
   （`...` 处按 4.1.3 展开的 32 个端点逐个写全。）
2. 打开生成的 nginx.conf，确认出现 `omni_proxy_prefill_groups 0:1 1:1 2:1 3:1;` 与 `omni_proxy_decode_groups 0:8 1:8 2:8 3:8;`（渲染逻辑在 [omni_proxy.sh:460-470](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/omni_proxy.sh#L460-L470)，注意逗号被替换成了空格）。
3. 故意把 decode 改成 `"0:32"` 再渲染并尝试 `nginx -t` 校验，预期命中 [ngx_http_omni_proxy_module.c:3426](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/ngx_http_omni_proxy_module.c#L3426) 的 `must contain the same unique group IDs`。
4. 再改成 `"0:16,1:16"`（id 集 {0,1}，段数 2），预期命中 [L3467](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/ngx_http_omni_proxy_module.c#L3467) 的 `number of decode upstream server ... and group ... not equal`。
5. 把验证通过的两组参数写进模板 `run_proxy_cmd` 的 `omni_proxy.sh` 调用里（追加两行），重跑 `--syntax-check`。

**需要观察的现象**：步骤 2 生成指令格式正确；步骤 3、4 分别报出两条不同的 EMERG 错误；步骤 5 语法检查通过。

**预期结果**：得到一份「4 prefill 组 × 各 1 台 + 4 decode 组 × 各 8 server」的配对方案，且通过全部三条 C 侧校验。真实 nginx 加载模块后的行为**待本地验证**（需要已编译的 `ngx_http_omni_proxy_module.so`）。

#### 4.2.5 小练习与答案

**练习 1**：`ENABLE_OMNI_CACHE=0` 时，这套 4P81D16 的 `kv-transfer-config` 是什么？

**答案**：走 else 分支（[L174-L178](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml#L174-L178) / [L309-L313](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml#L309-L313)）：connector 回落 `LLMDataDistConnector`，P 侧 `KV_PARALLEL_SIZE = PREFILL_POD_NUM + 1 = 5`、`kv_rank` 取各自 inventory 值 0/1/2/3；D 侧 `kv_rank = 4`、`KV_PARALLEL_SIZE = 5`。即恢复 u4-l1 的基线规则。

**练习 2**：为什么 INT8 模板里连一个 `--quantization` 参数都找不到，却仍然叫 int8 模板？

**答案**：因为量化信息由 jointfix 产物权重目录里 `config.json` 的 `quantization_config`（compressed-tensors 标准）声明，vLLM 加载时自动识别（u8-l4）；模板的 int8 体现在 `MODEL_PATH` 指向量化后的权重。`--dtype bfloat16` 保留是因为计算精度仍是 bfloat16，模板文件名与模型配置 json 名里的 `bf16` 指算子策略基线，与权重精度是两个维度。

**练习 3**：P 侧内存池 1500 GiB、D 侧 512 GiB，这两台机器的宿主机大页预留分别要设多少页？回退时又该设多少？

**答案**：2 MiB 一页，P 侧 1610612736000 / 2²¹ = 786,432 页；D 侧 549755813888 / 2²¹ = 262,144 页。回退时按 README_INT8 的规定统一执行 `set_hugepage_limit.sh --target-pages 262144`（即恢复 512 GiB 默认预留，[README_INT8.md:202-213](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README_INT8.md#L202-L213)）。

### 4.3 上线验证与回退：从大页预留到检查清单

#### 4.3.1 概念说明

「上线验证」不是跑通一次 curl 就结束，而是把部署拆成**可观察、可判定、可回退**的三段：

- **可观察**：每个特性都有专属日志/计数器——服务就绪看 `server_0.log` 的 `Application startup complete`；OmniCache 命中看 `num_computed_tokens` 与 `vllm:omni_cache_reuse_rate`（u7-l1）；APC 命中看 nginx access log 的命中字段（u6-l1）。
- **可判定**：每一步都有明确的通过/失败判据，失败走 u1-l5 的排障决策树。
- **可回退**：OmniCache 会**占住宿主机大页内存**，不释放就换别的配置会「可用内存不足或启动失败」——README_INT8 把这一点单独写成了一节警告。

#### 4.3.2 核心流程

505B 全特性上线的推荐时序（每步都有判定）：

```text
0. 前置：6 机 ssh 免密；镜像已 pull；INT8 权重已由 jointfix 产出并放到各机同路径
1. 大页预留（宿主机，一次性）
     P 机 786432 页 / D 机 262144 页          ← 判据：/proc/meminfo 的 HugePages_Total
2. --tags run_docker                          ← 判据：P/D/C 容器存在，docker ps 可见
3. --tags run_server                          ← 判据：run_prefill/run_decode.log 无早退；
     （脚本内自动 setup_hugetlbfs_2MB.sh 建池）    server_N.log 出现 Application startup complete
4. --tags run_proxy                           ← 判据：7000 端口监听；nginx_error.log 无 EMERG
5. 冒烟：单条 curl（model=openPangu-2.0-Pro）   ← 判据：200 且返回合理文本
6. 发压：梯度并发 + 长短 prompt 混合            ← 判据：TTFT/吞吐达标、无 5xx
7. 回退（如需）：stop_server → 重启容器 →
   set_hugepage_limit.sh --target-pages 262144 ← 判据：free -g 与 HugePages_ 计数回落
```

#### 4.3.3 源码精读

**README 的权威流程与三条命令**：启动镜像 `--tags run_docker`、拉起服务 `--tags run_server,run_proxy`（[README.md:102-137](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README.md#L102-L137)），OmniCache 形态则三条 tag 一次跑全（[README_INT8.md:194-200](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README_INT8.md#L194-L200)）。多机通信失败时的标准修复是显式 `export HCCL_SOCKET_IFNAME=<网卡名>`（[README.md:146-150](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README.md#L146-L150)；模板里默认由 ansible 用 `ip -4 route list 0/0` 自动探测，见 [omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml:994-1005](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml#L994-L1005)）。

**服务就绪判据的脚本化**：模板自带一个 300 秒轮询任务，靠 `docker exec ... grep -q "Application startup complete" .../server_0.log` 判定 D 侧就绪（[omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml:1109-1130](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml#L1109-L1130)，挂在 `proc_bind` tag 下、仅当 `proc_bind_enabled` 时生效）。人工排障时可直接复用这条 grep。可选的绑核收尾（`bind_cpu.sh`）在同一 tag 组里（[L1133-L1175](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml#L1133-L1175)），默认 `proc_bind_enabled | default(false)` 不启用。

**回退的官方三步**：[README_INT8.md:202-213](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README_INT8.md#L202-L213) 明确要求：① 重启容器释放占用；② 在代码根目录执行 `bash omni-cache/tools/setup/set_hugepage_limit.sh --target-pages 262144` 恢复默认预留；③ （隐含）验证内存已回来。脚本的用法与参数解析见 [set_hugepage_limit.sh:5-7](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-cache/tools/setup/set_hugepage_limit.sh#L5-L7)（`--target-pages=N` 手动指定 2MiB 页数，否则自动计算）与 [L20-L34](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-cache/tools/setup/set_hugepage_limit.sh#L20-L34)。停服用 `--tags stop_server`（模板生成 `kill_python_processes.sh` / `kill_ray_processes.sh`，[L860-L910](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml#L860-L910)）。

**请求侧判据**：向 C 节点 7000 端口发 OpenAI 兼容请求，`model` 必须等于 `SERVED_MODEL_NAME`（505B 模板里是 `openPangu-2.0-Pro`，[omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml:18](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml#L18)），否则 4xx；请求样例见 [README.md:152-176](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README.md#L152-L176)。

**每项特性对内存/吞吐的预期影响**（写进方案的「收益预期」栏）：

| 特性 | 内存影响 | 吞吐/时延影响 | 依据 |
| --- | --- | --- | --- |
| INT8 (W8A8) | 权重约 1.9× 压缩，HBM 富余 | 富余兑换更大批量/并发；INT8 KV Cache | u8-l1/u8-l4 |
| OmniCache | **占用宿主机大页**（P 1500 GiB、D 512 GiB），同时把 KV 预算扩到约 1476 GiB（`KV_CACHE_MEMORY_BYTES`） | 更长序列、更高并发、多轮对话 APC 命中率提升 | u7-l1 + 本模板 [L152-L164](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml#L152-L164) |
| 分组调度 | 无直接内存影响 | P/D 配对隔离，跨组 KV 迁移减少；请求粘滞到同 group | u6-l3 |
| 4P81D16 拓扑 | — | prefill 吞吐 ×4 实例；decode 32 DP server | 本讲 4.1 |

#### 4.3.4 代码实践

**实践目标**：演练「OmniCache → 普通部署」的回退闭环，并留下一份可复用的验证记录。

**操作步骤**：

1. 先确认脚本接口（不改动任何东西）：
   ```bash
   sed -n '1,40p' components/omni-cache/tools/setup/set_hugepage_limit.sh
   ```
   重点看 `--target-pages` 的两种写法（`--target-pages=N` 与 `--target-pages N`）与「非负整数」校验。
2. 在**任一台跑过 OmniCache 的机器**上记录回退前状态：
   ```bash
   free -g
   grep HugePages /proc/meminfo
   ```
3. 重启对应容器释放占用（README_INT8 第 1 步）。
4. 在代码根目录执行恢复命令（README_INT8 第 2 步）：
   ```bash
   bash omni-cache/tools/setup/set_hugepage_limit.sh --target-pages 262144
   ```
5. 再次执行步骤 2 的两条命令，记录 `HugePages_Total` 回到 262144、`HugePages_Free` 接近总量、`free -g` 的 available 回升。
6. 把「回退前 / 回退后」两组数字写进你的部署方案文档的回退章节。

**需要观察的现象**：`HugePages_Total` 从 786432（P 机）或 262144 以上（D 机，若曾手动调高）回到 262144；宿主机可用内存恢复。

**预期结果**：回退后同一组容器能以 `ENABLE_OMNI_CACHE=0`（或直接换 BF16 模板）重新拉起。真实数值**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：发压时发现 4 个 P 实例里有一个 KV 事件端口不通，APC 会怎样？

**答案**：APC 是双端链路（proxy 侧 `--omni-proxy-model-path` + 引擎侧 `--kv-events-config`）。缺一个 P 的事件源，radix tree 里就没有那个实例的 KV 块视图，proxy 对落在该实例的请求的前缀匹配会恒空——不会报错，只是 APC 命中率 silently 下降（u6-l3 的「缺一端不报错但匹配恒空」）。排查办法：用 `ss -tlnp` 核对 9100/9110/9120/9130 四个端口是否都在监听（u4-l3 的端口核对法）。

**练习 2**：为什么模板里 D 侧要显式加 `--no-enable-prefix-caching`？

**答案**：u7-l1 的结论——OmniCache 形态下 D 侧强制关闭 vLLM 自带前缀缓存，因为前缀命中判定已由主机内存池 + proxy 的 radix tree 承担，D 侧再开一份会造成双重记账与块生命周期冲突；对照 BF16 模板 P/D 两侧用 `ENABLE_PREFIX_CACHING` 开关（[bf16:L94-L100](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance2P1D_505B_bf16_open.yml#L94-L100) 与 [L205-L211](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance2P1D_505B_bf16_open.yml#L205-L211)）。

**练习 3**：上线检查清单里，为什么「大页预留」要放在 `run_docker` 之前？

**答案**：`setup_hugetlbfs_2MB.sh` 在容器内建池时挂载 hugetlbfs 并逐页写零，能拿到多少页取决于宿主机内核的预留总量（u7-l3 的两层分工：`set_hugepage_limit.sh` 管内核预留、`setup_hugetlbfs_2MB.sh` 管池化）。预留不足时建池会拿不到预期容量，P 侧 1500 GiB 的 `MAP_SIZE_BYTES` 目标落空，KV 预算随之缩水。所以顺序是：先在宿主机把 `HugePages_Total` 提到 786432（P）/ 262144（D），再建容器、再拉服务。

## 5. 综合实践

**任务：产出一份《openPangu-2.0-Pro 505B 生产上线部署方案》文档。** 这份文档是本讲三项最小模块的合体交付物，也是你以后接手任何 PD 分离形态时的模板。按下述六个章节完成（全程可在无 NPU 的机器上起草，标注「待本地验证」的项留到真机阶段填写）：

1. **拓扑与端口规划**
   - 以 [omni_infer_inventory_used_for_4P81D16.yml](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_inventory_used_for_4P81D16.yml) 为底稿复制一份，把 7 个 `127.0.0.x` 占位换成真实 IP（记住 `d1` 的 `host_ip` 填 `d0`）。
   - 附上 4.1.3 的规划总账表，并补三行派生值：`PREFILL_POD_NUM=4`、`dp=32`、KV 事件端口 9100/9110/9120/9130。
   - 用 `--syntax-check` 与 `--list-tasks` 验证 inventory × 模板组合合法。

2. **模板 environment 改动清单**
   - 必改四项：`LOG_PATH`、`MODEL_PATH`（指向 jointfix INT8 产物）、`DOCKER_IMAGE_ID`、三个 `DOCKER_NAME_*`（对照 [README.md:78-98](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README.md#L78-L98) 的说明逐项写「旧值 → 新值 → 影响范围」）。
   - 可选项逐个写明默认值与调整入口：`enable_omni_cache`、`enable_host_mapping`、`omni_cache_prefill_mmap_file`、`enable_omni_cache_dsa_split`、`omni_cache_base_port`、`omni_cache_zmq_base_port`（全部来自 [模板 L19-L31](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml#L19-L31)）。
   - 明确写下 `KV_PARALLEL_SIZE` 的两套取值（OmniCache: P=1 / D=33；LLMDataDist: 两侧=5），并注明「日志回显 1/33 属正常」。

3. **大页与内存预算**
   - P 机 786,432 页（1500 GiB）、D 机 262,144 页（512 GiB）；写明以 `MAP_SIZE_BYTES` 字节数为准、模板注释（48GB/1000GB）与实际不符。
   - 列出 P 侧 `KV_CACHE_MEMORY_BYTES ≈ 1476 GiB` 的算式（82 GiB × 18）。
   - 写下 DSA 分池的规则（主池 80%、页数向上取整追加，[L296-L303](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml#L296-L303)）。

4. **分组调度配置**
   - 把 `--omni-proxy-prefill-groups "0:1,1:1,2:1,3:1"` 与 `--omni-proxy-decode-groups "0:8,1:8,2:8,3:8"` 加进 `run_proxy_cmd`，附 dry-run 渲染出的 nginx 指令片段与三条校验的通过证据。
   - 说明运行期语义：一条请求的 decode 必须落在与其 prefill 同 group 的 8 个 decode server 里。

5. **发压验证方案**
   - 冒烟：单条 `curl`（`model: "openPangu-2.0-Pro"`，非流式 + 流式各一次）。
   - 就绪判据：4 台 P 的 `server_0.log` 与 2 台 D 的 `server_0.log` 均出现 `Application startup complete`（可复用模板 [L1109-L1130](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml#L1109-L1130) 的 grep）。
   - 特性生效判据：OmniCache 看 `num_computed_tokens` / `vllm:omni_cache_reuse_rate`；APC 看 nginx access log 命中字段与 `Add Prefill/Decode peer (group=N)` 日志（u6-l3）。
   - 梯度发压：并发 1 → 8 → 32 → 96，长短 prompt 混合，记录 TTFT / 每 token 时延 / 吞吐 / 5xx 计数；对照 4.3.3 的收益预期表逐项核对（数值**待本地验证**）。

6. **回退步骤**
   - `--tags stop_server` 停服 → 重启容器 → `bash omni-cache/tools/setup/set_hugepage_limit.sh --target-pages 262144` → `grep HugePages /proc/meminfo` 与 `free -g` 验证。
   - 附「整链路回退到 BF16 基线」的路径：换 [omni_infer_server_template_performance2P1D_505B_bf16_open.yml](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance2P1D_505B_bf16_open.yml)（注意它推荐的 inventory 是 2P1D 而非 4P81D16，拓扑要一起换）。

**验收标准**：文档能让一个没读过本手册的同事照着把服务拉起来；所有「为什么是这个值」都能指到仓库里的某一行；所有未经验证的数字都带「待本地验证」标记。

## 6. 本讲小结

- **4P81D16 = 4 个单机 P 实例（TP16、kv_rank 0-3）+ 1 个双机 D 实例（每机 16 个 DP server、共 32 个）+ 1 个 C 节点**，共 6 台 A3；inventory 用「分组 + 唯一 kv_rank + 多机同 host_ip」三件套表达这套拓扑，模板用 `unique host_ip` 数出 `PREFILL_POD_NUM=4`、用设备列表拼串数出 `dp=32`。
- **特性组合是分层的**：INT8 住在权重目录的 `quantization_config` 里（模板零量化参数、`--dtype bfloat16` 保留）；OmniCache 住在 `environment` + 脚本 if 分支里；分组调度住在 `run_proxy_cmd` 的 `omni_proxy.sh` 参数里（当前模板未配，需按「段数 = server 数、两侧 id 集合相等」自行添加，4P/32D 的解是 `0:1,1:1,2:1,3:1` × `0:8,1:8,2:8,3:8`）。
- **OmniCache 会改写 KV 规则**：`KV_CONNECTOR` 换成 `OmniCacheConnector`，`KV_PARALLEL_SIZE` 从基线的 `PREFILL_POD_NUM+1=5` 变成 P 侧 1、D 侧 `dp+1=33`——排障时看到 1/33 不是配置错误。特性组合甚至换掉了整份模型最佳实践 json（`enable_mome_sp`、`use_rope_fusion_op`、融合采样器、`optimize_first_chunk`）。
- **数字以源码字节数为准**：P 池 1500 GiB / 786,432 页，D 池 512 GiB / 262,144 页（模板注释 48GB/1000GB 与字节数不符）；P 侧 KV 预算 ≈ 82 GiB × 18 ≈ 1476 GiB。
- **上线验证三段式**：可观察（就绪 grep、`omni_cache_reuse_rate`、APC 日志）、可判定（每步有通过判据，失败走 u1-l5 决策树）、可回退（`stop_server` → 重启容器 → `set_hugepage_limit.sh --target-pages 262144` → `HugePages_` 计数验证）。
- **跨模板复用要逐变量核对语义**：`DECODE_POD_NUM` 在 505B 两份模板里分别是机器数与实例数；`PYTHONHASHSEED` 必须是 123（APC 前提）而不是 BF16 模板的 1234；`--shm-size` 从 500g 提到 1600g。

## 7. 下一步学习建议

本讲是学习手册的最后一篇正文讲义，此后建议沿三个方向继续：

1. **把方案跑起来**：按第 5 节的文档真机执行一遍，重点回填所有「待本地验证」的数字；遇到多机建链卡住，回到 u1-l5 的 HCCL 排障与 u4-l3 的端口矩阵核对法。
2. **向运行期深挖**：如果生产上出现 MoE 专家负载不均（decode 长尾），进入 u9 的 OmniPlacement：先读 [components/omni-eplb/README.md](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/README.md) 的启用配置，再读 u9-l2 的 `OmniPlanner` 源码；注意冗余专家与 AllGather 通信互斥的硬约束——本模板 P 侧 `moe_comm_strategy` 用的是 `all2allv`，是兼容的前提。
3. **向二次开发深挖**：若要在该形态上改推理行为，回到 u10-l3 的扩展点地图选型（改行为选 patch、换算子策略选 model_config json、换 KV 链路选 connector），并配合 u10-l2 的测试体系为改动补单测；改完用 `--tags run_server` 单独重拉即可生效（无需重建容器，u1-l4 的结论）。
