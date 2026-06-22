# MEXC spot WS protobuf schema (vendored)

`mexc_spot_depth.proto` is a minimal self-contained subset of MEXC's official
spot WebSocket protobuf schema (github.com/mexcdevelop/websocket-proto), sufficient
to decode the `spot@public.limit.depth.v3.api.pb@<SYMBOL>@5` channel on
`wss://wbs-api.mexc.com/ws`. Field numbers match MEXC's official protos
(PushDataV3ApiWrapper.publicLimitDepths = 303; asks=1/bids=2; price=1/quantity=2);
unknown wrapper fields are ignored, so the full 15-proto import closure is not needed.

The generated module lives at `tradinebotte-cex/mexc_spot_depth_pb2.py` (cex root, so
it deploys flat next to api_mexc.py). Regenerate after editing the .proto:

    pip install "protobuf>=5,<6" grpcio-tools
    python -m grpc_tools.protoc -Itradinebotte-cex/mexc_proto \
        --python_out=tradinebotte-cex tradinebotte-cex/mexc_proto/mexc_spot_depth.proto

Runtime dependency: `protobuf>=5,<6` (in requirements.txt). The generated _pb2 is
pinned to that major; bump both together.
