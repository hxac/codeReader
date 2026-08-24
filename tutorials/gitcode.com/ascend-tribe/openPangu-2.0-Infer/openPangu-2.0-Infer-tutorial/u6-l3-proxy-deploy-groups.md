# 在 ansible 体系里部署 proxy 与分组调度

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 `--tags run_proxy` 背后 ansible 做了哪三件事：渲染脚本、注入环境、进容器执行 `omni_proxy.sh`。
2. 读懂 `run_proxy_cmd` 里 prefill/decode 上游列表的生成算法，并能为自己的拓扑手工算出最终 endpoints。
3. 掌握 `omni_proxy_prefill_groups` / `omni_proxy_decode_groups` 的 `g:n` 段式语法、三条源码级校验规则，以及为什么「prefill 分 3 组、decode 分 1 组」这种写法会让 proxy 启动失败。
4. 理解 `USE_OMNI_PROXY` 开关的两条分支（omni_proxy.sh 与旧版 global_proxy.sh），以及 APC 感知调度完整的开启链路（模型路径 → KV 事件端口偏移 → ZMQ 订阅 → radix tree）。

## 2. 前置知识

- **ansible tag 与模板变量**：playbook 里的 `vars`（如 `run_proxy_cmd`）是带 Jinja2 占位符的 bash 脚本模板，`copy` 任务在执行时才把它渲染成真实脚本写到目标机（回顾 u1-l4 的「inventory 管机器、playbook 定义任务、tag 决定执行哪段」）。
- **`docker exec -e` 与容器环境分层**：脚本里的 `$PROXY_NODE_PORT` 来自 `docker run -e`（容器创建期），`$PREFILL_POD_NUM`、`$MODEL_PATH` 等来自 `docker exec -e`（执行期），两者都能被容器内脚本读到（u1-l4 的三层环境变量模型）。
- **upstream 与 server**：nginx 的 `upstream` 块是一组后端 `server ip:port` 的逻辑集合。omni-proxy 在此基础上给每个 server 附加一个 `group_id`，调度时请求的 prefill 和 decode 必须落在同一个 group（u6-l2 的「decode 必须同 group_id 硬约束」）。
- **APC（前缀缓存感知）**：proxy 内嵌 tokenizer 把请求 prompt 转成 token，再按与推理引擎相同的哈希算法在 radix tree 里匹配各节点的 KV 前缀缓存（u6-l1/u6-l2）。它依赖两件事：引擎侧通过 ZMQ 广播 KV 事件、proxy 侧加载模型 tokenizer。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml) | 92B 1P1D BF16 部署模板：`run_proxy_cmd` 基线写法与 run_proxy 标签任务 |
| [tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml) | 505B 4P1D INT8+OmniCache 模板：`USE_OMNI_PROXY` 开关与 KV 事件配置的完整接线 |
| [tools/ansible/92B/omni_infer_inventory_used_for_3P1D.yml](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_inventory_used_for_3P1D.yml) | 3P1D 拓扑清单：3 个 P 实例 + 1 个 D 实例（16 卡），综合实践的靶场 |
| [components/omni-proxy/omni_proxy/omni_proxy.sh](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/omni_proxy.sh) | proxy 配置生成与生命周期脚本：参数解析 → 渲染 nginx.conf → 启停/热加载 |
| [components/omni-proxy/omni_proxy/README_CN.md](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/README_CN.md) | proxy 官方文档：APC 开启说明与分组调度配置说明 |
| [components/omni-proxy/omni_proxy/modules/ngx_http_omni_proxy_module.c](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/ngx_http_omni_proxy_module.c) | nginx 主模块：分组指令解析与启动期校验（本讲的「裁判」） |
| [components/omni-proxy/omni_proxy/modules/omni_scheduler.c](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/omni_scheduler.c) | 调度器：同 group 硬约束的运行期体现 |
| [components/omni-proxy/omni_proxy/tests/test_proxy_group.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/tests/test_proxy_group.py) | 分组功能的官方测试：dry-run 断言与日志解析，是本讲实践的重要依据 |
| [tools/scripts/pd_run.sh](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/pd_run.sh) | P/D 侧引擎拉起脚本（4.3 节核对文档与源码差异时用到） |

## 4. 核心概念与源码讲解

### 4.1 ansible run_proxy：从模板变量到 omni_proxy.sh

#### 4.1.1 概念说明

u1-l4 部署服务时我们把 `run_proxy` 当成一个黑盒 tag。本模块把它拆开：`run_proxy` 标签触发的不是一条命令，而是一条「渲染 → 注入 → 执行」的流水线，最终在 C 节点容器里调用 `omni_proxy.sh` 生成 nginx.conf 并拉起 nginx。理解这条流水线，你才能知道改哪个变量会影响 proxy 行为、日志去哪里找。

#### 4.1.2 核心流程

`ansible-playbook --tags run_proxy` 执行时（以 92B 模板为例）：

1. **收集上游信息**（`tags: always` 的 `Register all values` 任务）：从 inventory 的 P/D 组算出 `PREFILL_API_SERVER_LIST` 与 `DECODE_API_SERVER_LIST_ALL` 两个 fact。
2. **渲染脚本**：`copy` 任务把 `vars` 里的 `run_proxy_cmd` 模板（含 Jinja2 占位符）渲染成 C 节点宿主机上的 `$SCRIPTS_PATH/run_proxy_server.sh`。
3. **注入环境并执行**：`docker exec -e PREFILL_POD_NUM=... -e MODEL_PATH=... -d $DOCKER_NAME_C /bin/bash -c $SCRIPTS_PATH/run_proxy_server.sh`，脚本先杀掉容器内旧 nginx，再展开 endpoints、调用 `omni_proxy.sh`。
4. **omni_proxy.sh 收尾**：解析约 40 个参数 → 生成 `/usr/local/nginx/conf/nginx.conf` → 先杀已有 nginx → `nginx -c` 启动。

数据流示意：

```text
inventory (P/D/C 组)
   │  ansible set_fact
   ▼
PREFILL_API_SERVER_LIST / DECODE_API_SERVER_LIST_ALL
   │  Jinja2 渲染进 run_proxy_cmd
   ▼
$SCRIPTS_PATH/run_proxy_server.sh  ──docker exec──▶  C 容器
   │  bash 循环展开 ip:port@num
   ▼
--prefill-endpoints / --decode-endpoints
   │  omni_proxy.sh 渲染
   ▼
nginx.conf (upstream + omni_proxy_* 指令) ──▶ nginx
```

#### 4.1.3 源码精读

**第一步：上游列表怎么来。** `Register all values` 任务用 Jinja2 遍历 inventory 组生成两个 fact：

- [omni_infer_server_template_performance1P1D_92B_bf16_open.yml:L584-L598](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L584-L598)：遍历 `groups['P']`，只取 `ansible_host == host_ip`（实例主节点）的机器，拼成 `ip:api_port` 逗号串——**一个 P 实例一个条目**。
- [omni_infer_server_template_performance1P1D_92B_bf16_open.yml:L600-L614](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L600-L614)：遍历 `groups['D']`，拼成 `ip:api_port@卡数` 形式，其中 `num` 是 `ascend_rt_visible_devices` 里逗号个数（16 卡即 15）。这个 `@num` 是压缩编码：D 实例是 DP 池，一台机器上有 16 个 TP1 server，各占连续端口。

**第二步：脚本里的展开循环。** `run_proxy_cmd` 把 `@num` 编码还原成逐个 server：

- [omni_infer_server_template_performance1P1D_92B_bf16_open.yml:L256-L273](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L256-L273)：对每个 `ip:port@num` 条目，循环 `num+1` 次生成 `ip:port, ip:(port+1), ...`。例如 `10.1.1.4:9100@15` 展开成 `10.1.1.4:9100` 到 `10.1.1.4:9115` 共 16 个 decode endpoint。prefill 列表不需展开（一实例一端点）。

**第三步：调用 omni_proxy.sh。**

- [omni_infer_server_template_performance1P1D_92B_bf16_open.yml:L275-L291](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L275-L291)：进入 `/workspace/omniinfer/components/omni-proxy/omni_proxy/` 调用脚本，关键参数：`--listen-port $PROXY_NODE_PORT`（即 inventory 的 `proxy_port` 7000，经 `docker run -e` 进入容器，见 [L362-L365](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L362-L365)）、`--prefill-endpoints` / `--decode-endpoints`、`--prefill-pod-size` / `--decode-pod-size`（每实例机器数，渲染为 nginx 指令 `prefill_pod_size` / `decode_pod_size`）、`--omni-proxy-pd-policy sequential` 以及与引擎侧对齐的批量参数（`--omni-proxy-max-batch-num-token 100000`、`--omni-proxy-prefill-max-num-seqs 4`、`--omni-proxy-decode-max-num-seqs 3`，分别对应模板里 P/D 的 `--max-num-batched-tokens` 与 `--max-num-seqs`）。注意 1P1D 模板**没有**传分组参数——单 P 实例无需分组。

**第四步：ansible 任务三连。**

- [omni_infer_server_template_performance1P1D_92B_bf16_open.yml:L990-L1011](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L990-L1011)：先生成并执行 `kill_nginx_processes.sh` 清掉容器里旧 nginx。
- [omni_infer_server_template_performance1P1D_92B_bf16_open.yml:L1013-L1022](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L1013-L1022)：`copy` 渲染 `run_proxy_server.sh`，`when: "'C' in group_names"` 限定只在 C 节点执行；tag 同时挂了 `run_proxy`、`run_server`、`delete_node`，所以 u1-l4 里跑 `--tags run_server,run_proxy` 时 proxy 会随引擎一起重启。
- [omni_infer_server_template_performance1P1D_92B_bf16_open.yml:L1024-L1034](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L1024-L1034)：经 [docker_start_proxy_cmd_c（L413-L419）](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L413-L419) 用 `docker exec -e` 注入 `PREFILL_POD_NUM`、`DECODE_POD_NUM`、`MODEL_PATH` 后执行脚本。

**omni_proxy.sh 侧的收尾。** 脚本默认值里分组参数为空字符串（[omni_proxy.sh:L29-L30](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/omni_proxy.sh#L29-L30)），解析 `--omni-proxy-prefill-groups` / `--omni-proxy-decode-groups`（[L190-L197](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/omni_proxy.sh#L190-L197)）。启动入口 `do_start` 的顺序是：校验必填 → 生成配置 → dry-run 则退出 → 否则先杀旧 nginx 再启动（[omni_proxy.sh:L649-L672](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/omni_proxy.sh#L649-L672)）。注意普通启动路径中 `nginx -t` 校验被注释掉了（[L307-L316](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/omni_proxy.sh#L307-L316)），而热加载路径 `do_reload` 强制 `nginx -t`、失败自动回滚备份配置（[L318-L335](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/omni_proxy.sh#L318-L335)）——所以分组写错时，**首次启动的报错只会出现在 nginx 自己的启动输出/error 日志里**，reload 路径反而更安全。

#### 4.1.4 代码实践

**实践目标**：不依赖 NPU 和真实集群，验证 `omni_proxy.sh` 的 dry-run 模式能渲染出完整 nginx.conf，并看清 ansible 传参到 nginx 指令的映射。

**操作步骤**：

1. 进入仓库（容器内或装有 bash 的任意 Linux 环境；不需要 nginx 在跑）。
2. 执行（示例代码，仿照 [tests/test_proxy_group.py:L79-L97](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/tests/test_proxy_group.py#L79-L97) 的官方用法）：

   ```bash
   cd components/omni-proxy/omni_proxy/
   bash omni_proxy.sh \
     --nginx-conf-file /tmp/my_nginx.conf \
     --core-num 1 \
     --listen-port 7000 \
     --prefill-endpoints 10.1.1.1:9000 \
     --decode-endpoints 10.1.1.4:9100,10.1.1.4:9101 \
     --omni-proxy-pd-policy sequential \
     --prefill-pod-size 1 --decode-pod-size 1 \
     --dry-run
   ```

3. 打开 `/tmp/my_nginx.conf`，找到 `upstream prefill_endpoints`、`upstream decode_endpoints`、`location ~ ^/v1(/chat)?/completions$` 三处。
4. 建立映射表：`--listen-port` → `listen ... reuseport;`，`--prefill-endpoints` → upstream 的 `server` 行，`--omni-proxy-pd-policy` → `omni_proxy_pd_policy` 指令，`--prefill-pod-size` → `prefill_pod_size` 指令。

**需要观察的现象**：

- 每个 endpoint 渲染成一行 `server ip:port max_fails=3 fail_timeout=10s;`（由 [gen_upstream_block，omni_proxy.sh:L374-L387](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/omni_proxy.sh#L374-L387) 生成）。
- 未传分组参数时，conf 里**没有** `omni_proxy_prefill_groups` 行。

**预期结果**：dry-run 输出 `nginx.conf generated at ...` 与 `Dry run complete.`，conf 内容与上述映射一致。若你的环境因 `set -e` 或写权限失败，改用可写的 `--nginx-conf-file` 路径。完整行为待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：3P1D 拓扑（见 [omni_infer_inventory_used_for_3P1D.yml](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_inventory_used_for_3P1D.yml)）中，`PREFILL_API_SERVER_LIST` 和 `DECODE_API_SERVER_LIST_ALL` 分别是什么？

**答案**：三个 P 主机 `ansible_host` 各不相同且等于自身 `host_ip`，`api_port = 9000 + kv_rank×10`（kv_rank 为 0/1/2），所以 prefill 列表是 `P0_IP:9000,P1_IP:9010,P2_IP:9020`；D 只有一台 16 卡机，条目为 `D_IP:9100@15`，经脚本展开成 `D_IP:9100` 到 `D_IP:9115` 共 16 个 decode endpoint。

**练习 2**：为什么 `run_proxy_cmd` 里能直接用 `$PROXY_NODE_PORT`，而这个变量从未出现在 `docker_start_proxy_cmd_c` 的 `-e` 列表里？

**答案**：`PROXY_NODE_PORT` 是在 `run_docker` 阶段创建 C 容器时通过 [start_docker_cmd_c 的 `-e PROXY_NODE_PORT=$NODE_PORT`（L362-L365）](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L362-L365) 写进容器环境的；`PREFILL_POD_NUM` 等则是每次 `docker exec -e` 时注入。两层环境对容器内脚本等效，但重建容器才会改变前者。

**练习 3**：想在不动引擎的情况下重载 proxy 配置，用哪个入口？它比首次启动多了什么保护？

**答案**：92B 模板提供了 `reload_proxy` 标签（[L1036-L1053](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L1036-L1053)），其 `reload_proxy_cmd` 追加 `--reload`。`do_reload` 会先 `nginx -t` 校验新配置，失败则用 `_bak` 备份回滚（[omni_proxy.sh:L318-L335](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/omni_proxy.sh#L318-L335)）；而首次启动路径的 `nginx -t` 是被注释掉的。

### 4.2 分组映射：omni_proxy_prefill_groups / decode_groups

#### 4.2.1 概念说明

当部署里有多个 P 实例和多个 D 实例时，可能会希望把「某个 P 和某些 D」绑定成一个小池子：请求在组内完成 prefill→decode，KV 传输路径也固定在组内。分组调度就是把这个拓扑意愿表达成两条 nginx 指令：`omni_proxy_prefill_groups` 和 `omni_proxy_decode_groups`。它们描述的不是负载均衡权重，而是**静态的归属关系**。

#### 4.2.2 核心流程

语法是「段式列表」，每段 `分组id:数量`，按 upstream 中 server 的书写顺序依次消费：

```text
omni_proxy_prefill_groups 0:2 1:1 2:1;
        ▲     ▲
        │     └── 该段覆盖接下来的 1 个 server
        └── 这些 server 属于分组 0（共 2 个）
```

解析与生效过程：

1. nginx 读配置时把 `0:2 1:1 2:1` 展开成长度为 5 的数组 `[0,0,1,2]`，与 upstream 里第 i 个 server 一一对应。
2. 启动期做三条校验（见 4.2.3），任何一条不过则 nginx 拒绝启动。
3. 运行期调度：请求先选 prefill（同时记下它的 `group_id`），再在该 group 的 decode 候选里选 decode。

两条硬性关系（源码验证）：

- 段内数量之和 = 该侧 upstream 的 server 总数；
- 两侧去重后的分组 id 集合必须完全相同。

#### 4.2.3 源码精读

**参数渲染**：omni_proxy.sh 把命令行逗号串转成空格分隔的 nginx 指令——[omni_proxy.sh:L460-L470](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/omni_proxy.sh#L460-L470) 用 `${var//,/ }` 归一化后拼成 `omni_proxy_prefill_groups 0:2 1:1 2:1;`，渲染进 location 块（[L546-L547](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/omni_proxy.sh#L546-L547)）。用法说明见 [README_CN.md:L182-L189](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/README_CN.md#L182-L189)。

**指令解析与展开**：每段 `g:n` 在 [omni_proxy_parse_group_token（ngx_http_omni_proxy_module.c:L2827-L2853）](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/ngx_http_omni_proxy_module.c#L2827-L2853) 中按 `:` 拆成 `group_id` 与 `repeat`（要求 `g ≥ 0` 且 `n > 0`），再由 [omni_proxy_set_groups（L2855-L2893）](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/ngx_http_omni_proxy_module.c#L2855-L2893) 循环 `repeat` 次把 `group_id` 压进数组——数组第 i 位就是 upstream 第 i 个 server 的分组。查表函数 [omni_proxy_get_group_id（L2969-L2989）](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/ngx_http_omni_proxy_module.c#L2969-L2989) 还揭示了默认行为：**未配置分组时所有 server 返回 group 0**，即「全员一组」。

**三条启动期校验**（[omni_proxy_check_upstream_group_setting，L3404-L3478](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/ngx_http_omni_proxy_module.c#L3404-L3478)）：

1. **成对出现**（L3408-L3422）：只配一侧、另一侧缺失，报 `... is set but ... is missing`，启动失败。
2. **id 集合相等**（[omni_groups_equal_unique，L3357-L3401](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/ngx_http_omni_proxy_module.c#L3357-L3401)）：两侧数组排序去重后逐元素比较，不同则报 `must contain the same unique group IDs`。
3. **数量相等**（L3456-L3469）：展开后的段数必须分别等于 prefill/decode upstream 的 server 数，报 `number of prefill upstream server N and group M not equal`。

**⚠️ 文档与源码的两处偏差（以源码为准）**：[README_CN.md:L147-L153](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/README_CN.md#L147-L153) 说「未覆盖的 server 会自动分配到分组 0」，但当前源码在数量不等时直接报错拒绝启动；README 示例（[L175-L176](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/README_CN.md#L175-L176)）prefill 写 `0:2 1:1 1:1`（唯一 id 为 {0,1}）配 decode `0:2 1:4 2:2`（唯一 id 为 {0,1,2}），按校验 2 也无法通过。正确写法参考官方测试 [test_proxy_group.py:L21-L22](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/tests/test_proxy_group.py#L21-L22)：prefill `0:2,1:1,2:1` 配 decode `0:1,1:2,2:1`，两侧唯一 id 都是 {0,1,2}。这提示我们：README 描述可能对应旧版本行为，改配置前先看源码校验逻辑。

**运行期硬约束**：调度器选 decode 时过滤不同组的候选——[omni_scheduler.c:L901](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/omni_scheduler.c#L901) 与 [L936](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/omni_scheduler.c#L936) 都是 `if (decode->comm.group_id != req->prefill_group_id) continue;`。

**日志验证点**：每个 server 注册时打印 NOTICE 级日志（[ngx_http_omni_proxy_module.c:L3102-L3104](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/ngx_http_omni_proxy_module.c#L3102-L3104)）：

```text
Add Prefill peer 0 in endpoint[0] -> 10.1.1.1:9000 (group=0)
Add Decode peer 15 in endpoint[15] -> 10.1.1.4:9115 (group=2)
```

官方测试就用正则 `Add Prefill peer \d+ in endpoint\[(\d+)\] .*? \(group=(\d+)\)` 解析这行日志做断言（[test_proxy_group.py:L446-L481](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/tests/test_proxy_group.py#L446-L481)）。模板的 `--log-level info`（92B）/`notice`（505B）都会包含 notice 级消息，日志文件即 `run_proxy_cmd` 里指定的 `nginx_error.log`。

#### 4.2.4 代码实践

**实践目标**：为 3P1D 写出合法的分组配置，并用 dry-run 验证渲染结果、用「故意写错」验证源码校验确实生效。

**操作步骤**：

1. **算数量**：3P1D 的 prefill endpoints 有 3 个（每实例 1 个），decode endpoints 有 16 个（1 台 16 卡机展开）。
2. **设计分组**：想让 3 个 P 各成一组，prefill 侧写 `0:1,1:1,2:1`（唯一 id {0,1,2}）。注意：**不能**给 decode 只写 `0:16`——校验 2 要求两侧 id 集合相同，`{0}` ≠ `{0,1,2}` 会启动失败。decode 侧必须也出现 0、1、2 三个 id，把 16 个 server 划成三份，例如 `0:6,1:5,2:5`（6+5+5=16，满足校验 3）。
3. **dry-run 验证**（示例代码）：

   ```bash
   bash omni_proxy.sh \
     --nginx-conf-file /tmp/group_3p1d.conf \
     --prefill-endpoints 10.1.1.1:9000,10.1.1.2:9010,10.1.1.3:9020 \
     --decode-endpoints 10.1.1.4:9100,10.1.1.4:9101,...,10.1.1.4:9115 \
     --omni-proxy-prefill-groups "0:1,1:1,2:1" \
     --omni-proxy-decode-groups "0:6,1:5,2:5" \
     --dry-run
   grep "omni_proxy_.*_groups" /tmp/group_3p1d.conf
   ```

4. **负向验证**（需要环境里有 nginx 可执行文件；参照 [test_prefill_group_num_not_match_dry_run，test_proxy_group.py:L103-L127](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/tests/test_proxy_group.py#L103-L127)）：把 prefill 改成 `0:1,1:1`（只覆盖 2 个 server），dry-run 生成 conf 后执行 `nginx -c /tmp/bad.conf -t`，断言返回非 0 并观察报错文案。

**需要观察的现象**：第 3 步 conf 中出现 `omni_proxy_prefill_groups 0:1 1:1 2:1;` 与 `omni_proxy_decode_groups 0:6 1:5 2:5;`；第 4 步 `nginx -t` 输出 `number of prefill upstream server 3 and group 2 not equal` 之类的 EMERG 报错。

**预期结果**：合法配置通过渲染与校验；非法配置被拒绝。dry-run 部分待本地验证（负向验证依赖 nginx 存在于 PATH）。

#### 4.2.5 小练习与答案

**练习 1**：`omni_proxy_prefill_groups 0:2 1:1 1:1` 展开后的数组是什么？第 3、4 个 server 各属哪组？

**答案**：展开为 `[0,0,1,1]`。第 1、2 个 server 属组 0，第 3、4 个都属组 1（注意不是「第 3 个属 1、第 4 个属 2」——README 的这段文字描述与它自己给的配置不符，正确语义以 [omni_proxy_set_groups](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/ngx_http_omni_proxy_module.c#L2877-L2890) 的展开逻辑为准）。

**练习 2**：2 个 P 实例 + 2 台 D 机（每台 16 卡，共 32 个 decode endpoint），想让「P0 配 D0 的 16 server、P1 配 D1 的 16 server」，分组怎么写？

**答案**：prefill `0:1,1:1`；decode 侧前 16 个是 D0、后 16 个是 D1（endpoint 顺序与 `--decode-endpoints` 书写顺序一致），写 `0:16,1:16`。两侧唯一 id 都是 {0,1}，段数 32 = server 数 32，三条校验全过。

**练习 3**：为什么调度器要坚持「decode 与 prefill 同组」这一硬约束？

**答案**：PD 分离下请求的 KV Cache 要从被选中的 P 传到被选中的 D。若允许跨组，P 与 D 的配对关系就退化为全连接，组内亲和（例如同组 P/D 网络更近、或 OmniCache 多 P 场景下缓存归属明确）失效。源码层面这由 [omni_scheduler.c:L901、L936](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/omni_scheduler.c#L936) 的候选过滤保证。

### 4.3 APC 开关：USE_OMNI_PROXY 与 KV 事件链路

#### 4.3.1 概念说明

APC 感知调度（u6-l1/u6-l2 讲过的 radix tree 前缀匹配）不是「装好 proxy 就自动有」，它需要一条从引擎到 proxy 的完整数据链：引擎侧把 KV 块事件用 ZMQ 广播出来，proxy 侧加载模型 tokenizer、订阅这些事件、为每个 upstream 维护 radix tree。这条链在部署面上由三个开关串起来：`USE_OMNI_PROXY`（选 proxy 实现）、`--omni-proxy-model-path`（给 proxy 配 tokenizer）、`--kv-events-config`（让引擎发事件）。

#### 4.3.2 核心流程

```text
play environment: USE_OMNI_PROXY=1 ──docker exec -e──▶ run_proxy_cmd
   ├─ =1 → omni_proxy.sh（第二代，含 --omni-proxy-model-path）
   └─ ≠1 → global_proxy.sh（第一代，lb_sdk 参数）

P 侧引擎: ENDPOINT_PORT = api_port + 100
   └─ --kv-events-config {"enable_kv_cache_events":true,"publisher":"zmq",
                           "topic":"kv-events","endpoint":"tcp://*:ENDPOINT_PORT"}

proxy 侧: --omni-proxy-model-path → nginx.conf 生成
   ├─ omni_proxy_model_path "<MODEL_PATH>";
   └─ omni_proxy_vllm_kv_port_offset 100;

nginx worker 启动: 对分到的每个 prefill upstream
   └─ ZMQ connect 到 ip:(api_port+100)，KV 事件落入该 upstream 的 radix tree
```

两个「100」是这条链的咬合点：引擎在 `api_port+100` 发布，proxy 按 `api_port + vllm_kv_port_offset(100)` 订阅。

#### 4.3.3 源码精读

**USE_OMNI_PROXY 开关**：505B 模板在 play environment 里声明 [USE_OMNI_PROXY: "1"（L40-L43）](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml#L40-L43)（同处还定义了第一代 proxy 用的 `PREFILL_LB_SDK`/`DECODE_LB_SDK`），经 [docker_start_proxy_cmd_c 的 `-e USE_OMNI_PROXY=$USE_OMNI_PROXY`（L560-L567）](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml#L560-L567) 进入容器。`run_proxy_cmd` 里用它做分支（[L351-L417](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml#L351-L417)）：

- `=1` 分支（[L383-L401](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml#L383-L401)）：走 omni_proxy.sh，带 `--omni-proxy-model-path $MODEL_PATH`、`--omni-proxy-tokenize-chunk-bytes 16384` 等新代参数。
- else 分支（[L402-L417](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml#L402-L417)）：退回第一代 `global_proxy.sh`，用 `--prefill-lb-sdk`/`--decode-lb-sdk` 负载均衡插件。

**引擎侧发事件**：P 侧脚本计算 `ENDPOINT_PORT = api_port + 100` 并拼出 KV_EVENTS_CONFIG，作为 `--kv-events-config` 传入 vLLM（[L111-L113](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml#L111-L113)）：`{"enable_kv_cache_events":true,"publisher":"zmq","topic":"kv-events","endpoint":"tcp://*:9100"}`（以 api_port 9000 为例）。

**proxy 侧收事件**：`omni_proxy.sh` 只在传了 `--omni-proxy-model-path` 时才生成两条指令（[L554-L559](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/omni_proxy.sh#L554-L559)）：`omni_proxy_model_path` 与硬编码的 `omni_proxy_vllm_kv_port_offset 100`。nginx worker 初始化时，[omni_proxy_init_kv_listener（ngx_http_omni_proxy_module.c:L3773-L3819）](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/ngx_http_omni_proxy_module.c#L3773-L3819) 对分给本 worker 的每个 prefill upstream，把 ZMQ 地址拼成 `ip:(port + vllm_kv_port_offset)` 并订阅（L3803-L3818）；upstream 注册时也只为配置了该偏移的实例创建 radix tree（[L3065-L3083](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/ngx_http_omni_proxy_module.c#L3065-L3083)）。

**⚠️ 文档滞后（重要排坑点）**：[README_CN.md:L139-L141](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/README_CN.md#L139-L141) 指引「在 pd_run.sh 中设置 `ENABLE_APC_EVENT=1`」，但检索当前 [tools/scripts/pd_run.sh](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/pd_run.sh) 全文不存在 `ENABLE_APC_EVENT` 这个变量——开启 KV 事件的实际机制是上面模板里的 `--kv-events-config`。另注意：92B BF16 模板在 [L91](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L91) 定义了 `KV_EVENTS_CONFIG`，但其 `EXTRA_ARGS`（[L92](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L92)）并未拼入该变量，也没有传 `--omni-proxy-model-path`——即 92B 基线模板实际未启用 APC 事件链，只有 505B OmniCache 模板把两端都接通了。

**哈希种子一致性**：proxy 的 block hash 链以 `PYTHONHASHSEED` 为链首（u6-l2），所以该值必须固定且两侧一致。505B 模板容器与 proxy 均用 `PYTHONHASHSEED=123`（[L52](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml#L52)、[L359](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml#L359)）；92B 模板统一用 1234（[L34](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L34)、[L250](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L250)）。关键是「同一部署内取值一致」，具体数值不重要。

#### 4.3.4 代码实践

**实践目标**：在源码层面把 APC 开启链路的每一跳找出来，形成一张可核对的清单，并识别两份模板谁真正启用了 APC。

**操作步骤**：

1. 在 505B 模板中依次定位并抄下：`USE_OMNI_PROXY` 的定义处（L43）、`docker exec -e` 传递处（L564）、`run_proxy_cmd` 分支判断处（L383）、`--omni-proxy-model-path` 实参处（L397）。
2. 在 P 侧脚本段定位 `ENDPOINT_PORT` 计算与 `--kv-events-config` 拼接（L111-L113）。
3. 在 omni_proxy.sh 定位 model path 触发的两条 nginx 指令（L554-L559）。
4. 在 C 模块定位订阅地址拼接 `port + vllm_kv_port_offset`（L3803-L3806）。
5. 对照检查 92B BF16 模板：确认 `KV_EVENTS_CONFIG`（L91）没有被拼进 `EXTRA_ARGS`（L92），且 `run_proxy_cmd` 未传 `--omni-proxy-model-path`。

**需要观察的现象**：清单上每一跳都能在源码中指出确切行号；92B 模板在「引擎发事件」和「proxy 收事件」两跳都是断开的。

**预期结果**：得出结论——APC 是「引擎发布 + proxy 订阅」的双端开关，只开一端不会报错但匹配永远为空（radix tree 没有数据）。链路核对属源码阅读，无需环境即可完成；运行期效果待本地验证（可在容器内用 `ss -tnp | grep <api_port+100>` 观察 ZMQ 连接是否建立）。

#### 4.3.5 小练习与答案

**练习 1**：`USE_OMNI_PROXY=0` 时部署会发生什么变化？

**答案**：`run_proxy_cmd` 走 else 分支，改用第一代 `global_proxy.sh` 拉起（带 `--prefill-lb-sdk pd_score_balance` 等参数），没有 tokenizer 复用、radix tree 等 omni_proxy 新特性；P/D 引擎侧不受影响。

**练习 2**：某集群 P 的 `api_port` 是 9010，proxy 侧应订阅哪个端口？这个 100 在两份代码里分别叫什么？

**答案**：订阅 9110。引擎侧是 `ENDPOINT_PORT = api_port + 100`（模板 L111）；proxy 侧是 `vllm_kv_port_offset`（omni_proxy.sh L557 硬编码为 100，订阅时在 module.c L3806 相加）。改任何一侧的偏移都必须同步另一侧，否则订阅到空端口。

**练习 3**：如果只给 proxy 加了 `--omni-proxy-model-path` 而 P 侧没传 `--kv-events-config`，请求还能正常转发吗？APC 会怎样？

**答案**：能正常转发——分组与负载调度不依赖 KV 事件；但 proxy 会去连 `ip:(api_port+100)` 上不存在的 ZMQ 端点，radix tree 得不到 BlockStored/Removed 事件，APC 匹配深度恒为 0，等价于关闭前缀缓存感知（参照 u6-l2：ZMQ 读事件是调度器的三条事件源之一）。

## 5. 综合实践

**任务：把 92B 1P1D 模板的 proxy 配置改造成 3P1D + 分组调度，并在日志中确认分组映射生效。**

前置：已按 u1-l4 完成 ssh 免密、镜像与路径配置，且已用 [omni_infer_inventory_used_for_3P1D.yml](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_inventory_used_for_3P1D.yml) 准备好 3 台 P 机 + 1 台 D 机 + 1 台 C 机。

1. **复制模板**：把 `omni_infer_server_template_performance1P1D_92B_bf16_open.yml` 另存为实验模板（放在你自己的管理目录，不改动仓库源文件）。
2. **改 run_proxy_cmd**：在 [omni_proxy.sh 调用段（原 L275-L291）](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L275-L291) 末尾追加两行参数（示例代码）：

   ```bash
   --omni-proxy-prefill-groups "0:1,1:1,2:1" \
   --omni-proxy-decode-groups "0:6,1:5,2:5"
   ```

   依据：3 个 prefill endpoint 各成一组（id 0/1/2）；16 个 decode endpoint 必须划成相同的三个 id（6+5+5=16），否则触发 [omni_groups_equal_unique](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/ngx_http_omni_proxy_module.c#L3357-L3401) 的「id 集合相等」校验而启动失败——题目原本设想的「decode 只分 1 组」正是本讲踩过的坑。
3. **先 dry-run 再上线**（可选但强烈推荐）：在 C 容器里手工执行改造后的脚本，末尾加 `--dry-run`，检查生成的 `/usr/local/nginx/conf/nginx.conf` 中两条分组指令与 upstream server 数量一致。
4. **部署**：`ansible-playbook -i inventory ... --tags run_docker` 后 `--tags run_server,run_proxy`。
5. **日志确认**：在 `LOG_PATH/<C节点>/nginx_error.log` 中执行：

   ```bash
   grep "Add Prefill peer" nginx_error.log   # 期望 3 行，group=0/1/2 各一
   grep "Add Decode peer"  nginx_error.log   # 期望 16 行，group=0 出现 6 次、1 出现 5 次、2 出现 5 次
   ```

6. **行为验证**：发若干请求后从 `nginx_access.log`（json 格式，字段见 [gen_access_log，omni_proxy.sh:L389-L433](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/omni_proxy.sh#L389-L433)）抽 `prefill_idx` 与 `decode_idx`，核对同一请求的两个 idx 是否落在你划分的同一 group（参考 [test_proxy_group.py:L446-L481](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/tests/test_proxy_group.py#L446-L481) 的解析方法）。
7. **回退**：删除两行分组参数后重跑 `--tags run_proxy`，确认日志回到「全员 group=0」。

说明：3P1D + LLMDataDist 场景下 D 是一个整体 DP 池，是否真的要按组切割 decode 取决于你的业务目标（组内亲和 vs 全局负载均衡）；本实践的重点是掌握配置语法、校验规则与日志验证手段。整体流程与性能影响待本地验证。

## 6. 本讲小结

- `run_proxy` 标签是一条「fact 收集 → Jinja2 渲染脚本 → docker exec 注入环境 → omni_proxy.sh 渲染 nginx.conf 并启停 nginx」的流水线；proxy 的全部行为差异都落在 `run_proxy_cmd` 这一个模板变量里，改它后重跑 tag 即生效，无需重建容器。
- 上游列表有两套编码：prefill 一实例一端点（`ip:api_port`），decode 用 `ip:port@卡数` 压缩、由脚本循环展开成逐 server 端点。
- 分组语法是段式 `分组id:数量`，按 server 顺序消费；源码级三条校验：成对配置、两侧 id 集合相等、段数总和等于 server 数——「prefill 3 组 + decode 1 组」这类不对称写法会直接启动失败，README 示例本身过不了当前校验，以源码为准。
- 分组的运行期语义是硬约束：请求的 decode 必须与 prefill 同 group；不配置分组时全员 group 0（等于无分组）。验证看 `Add Prefill/Decode peer ... (group=N)` 日志。
- APC 开启链 = `USE_OMNI_PROXY=1` + proxy 传 `--omni-proxy-model-path`（生成 `omni_proxy_vllm_kv_port_offset 100`）+ P 侧 `--kv-events-config`（在 `api_port+100` 发 ZMQ）；文档里的 `ENABLE_APC_EVENT` 在当前 pd_run.sh 中已不存在，92B 基线模板两端均未接线、505B OmniCache 模板才是完整示例。

## 7. 下一步学习建议

- 下一讲进入单元 7（omni-cache）：建议先读 [components/omni-cache/README.md](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-cache/README.md) 与 `examples/run_server_p.sh`，重点关注本讲 505B 模板里出现的 `ENABLE_OMNI_CACHE`、`BASE_PORT`、`ZMQ_BASE_PORT` 等变量的消费位置，它们与 proxy 的 KV 事件链同属「KV 在哪、谁看着」这一主题。
- 想继续深挖 proxy：阅读 [tests/run_proxy.py](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/tests/run_proxy.py) 与 [tests/README.md](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/tests/README.md)，用 mock 引擎在本机复现分组调度与 APC 匹配，这比真机实验便宜得多。
- 后续 u10-l4 生产综合实战会把本讲的分组调度与 INT8、OmniCache 组合进 505B 4P81D16 大拓扑，可提前对照 [omni_infer_inventory_used_for_4P81D16.yml](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_inventory_used_for_4P81D16.yml) 思考：4 个 P 实例的分组你会怎么划。
