// SPDX-License-Identifier: UNLICENSED
pragma solidity ^0.8.28;

import {Script, console} from "forge-std/Script.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {AgentPayChannel} from "../src/AgentPayChannel.sol";

/// forge script script/Deploy.s.sol --rpc-url $RPC_URL --private-key $KEY --broadcast
/// Env: TOKEN (ERC-20 address, e.g. USDC), CLOSE_DELAY (seconds, default 1 day).
contract Deploy is Script {
    function run() external returns (AgentPayChannel channel) {
        address token = vm.envAddress("TOKEN");
        uint64 delay = uint64(vm.envOr("CLOSE_DELAY", uint256(1 days)));
        vm.startBroadcast();
        channel = new AgentPayChannel(IERC20(token), delay);
        vm.stopBroadcast();
        console.log("AgentPayChannel:", address(channel));
    }
}
