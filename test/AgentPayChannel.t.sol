// SPDX-License-Identifier: UNLICENSED
pragma solidity 0.8.28;

import {Test} from "forge-std/Test.sol";
import {IERC1271} from "@openzeppelin/contracts/interfaces/IERC1271.sol";
import {AgentPayChannel} from "../src/AgentPayChannel.sol";
import {MockUSDC} from "../src/mocks/MockUSDC.sol";

/// @dev Smart-contract wallet that accepts signatures from one owner key (ERC-1271).
contract MockSmartWallet is IERC1271 {
    address public immutable owner;

    constructor(address owner_) {
        owner = owner_;
    }

    function isValidSignature(bytes32 hash, bytes calldata sig) external view returns (bytes4) {
        (bytes32 r, bytes32 s, uint8 v) = abi.decode(sig, (bytes32, bytes32, uint8));
        return ecrecover(hash, v, r, s) == owner ? IERC1271.isValidSignature.selector : bytes4(0);
    }

    function approve(MockUSDC token, address spender, uint256 amount) external {
        token.approve(spender, amount);
    }

    function deposit(AgentPayChannel channel, address provider, uint128 amount) external {
        channel.deposit(provider, amount);
    }
}

contract AgentPayChannelTest is Test {
    MockUSDC usdc;
    AgentPayChannel channel;

    uint256 payerKey = 0xA11CE;
    address payer;
    address provider = makeAddr("provider");
    address otherProvider = makeAddr("otherProvider");

    uint64 constant DELAY = 1 days;
    uint128 constant FUND = 100e6; // 100 USDC

    function setUp() public {
        usdc = new MockUSDC();
        channel = new AgentPayChannel(usdc, DELAY);
        payer = vm.addr(payerKey);
        usdc.mint(payer, 1_000e6);
        vm.prank(payer);
        usdc.approve(address(channel), type(uint256).max);
    }

    // ---------------------------------------------------------------- helpers

    function _deposit(uint128 amount) internal {
        vm.prank(payer);
        channel.deposit(provider, amount);
    }

    function _voucher(address to, uint256 cumulative, uint256 epoch)
        internal
        view
        returns (bytes memory)
    {
        return _voucherFrom(payerKey, payer, to, cumulative, epoch);
    }

    function _voucherFrom(uint256 key, address from, address to, uint256 cumulative, uint256 epoch)
        internal
        view
        returns (bytes memory)
    {
        bytes32 digest = channel.voucherDigest(from, to, cumulative, epoch);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(key, digest);
        return abi.encodePacked(r, s, v);
    }

    function _state(address from, address to)
        internal
        view
        returns (uint128 dep, uint128 claimed, uint64 closeAt, uint64 epoch)
    {
        return channel.channels(from, to);
    }

    // ---------------------------------------------------------------- deposit

    function test_deposit_escrowsFunds() public {
        _deposit(FUND);
        (uint128 dep, uint128 claimed, uint64 closeAt, uint64 epoch) = _state(payer, provider);
        assertEq(dep, FUND);
        assertEq(claimed, 0);
        assertEq(closeAt, 0);
        assertEq(epoch, 0);
        assertEq(usdc.balanceOf(address(channel)), FUND);
        assertEq(usdc.balanceOf(payer), 1_000e6 - FUND);
    }

    function test_deposit_topsUp() public {
        _deposit(40e6);
        _deposit(60e6);
        (uint128 dep,,,) = _state(payer, provider);
        assertEq(dep, 100e6);
    }

    function test_deposit_zeroReverts() public {
        vm.prank(payer);
        vm.expectRevert(AgentPayChannel.ZeroAmount.selector);
        channel.deposit(provider, 0);
    }

    function test_deposit_zeroProviderReverts() public {
        vm.prank(payer);
        vm.expectRevert(AgentPayChannel.ZeroAddress.selector);
        channel.deposit(address(0), 1);
    }

    function test_deposit_revertsWithoutAllowance() public {
        address broke = makeAddr("broke");
        vm.prank(broke);
        vm.expectRevert();
        channel.deposit(provider, 1);
    }

    function test_deposit_revertsWhileClosing() public {
        _deposit(FUND);
        vm.prank(payer);
        channel.requestClose(provider);
        vm.prank(payer);
        vm.expectRevert(AgentPayChannel.ChannelClosing.selector);
        channel.deposit(provider, 1);
    }

    // ------------------------------------------------------------------ claim

    function test_claim_paysProvider() public {
        _deposit(FUND);
        bytes memory sig = _voucher(provider, 30e6, 0);

        vm.prank(provider);
        channel.claim(payer, 30e6, sig);

        assertEq(usdc.balanceOf(provider), 30e6);
        assertEq(channel.unclaimed(payer, provider), 70e6);
    }

    function test_claim_incrementalVouchersPayOnlyTheDifference() public {
        _deposit(FUND);

        bytes memory sig1 = _voucher(provider, 10e6, 0);

        vm.prank(provider);
        channel.claim(payer, 10e6, sig1);
        bytes memory sig2 = _voucher(provider, 25e6, 0);
        vm.prank(provider);
        channel.claim(payer, 25e6, sig2);

        assertEq(usdc.balanceOf(provider), 25e6);
        (, uint128 claimed,,) = _state(payer, provider);
        assertEq(claimed, 25e6);
    }

    function test_claim_replayReverts() public {
        _deposit(FUND);
        bytes memory sig = _voucher(provider, 10e6, 0);
        vm.prank(provider);
        channel.claim(payer, 10e6, sig);

        vm.prank(provider);
        vm.expectRevert(abi.encodeWithSelector(AgentPayChannel.NotIncreasing.selector, 10e6, 10e6));
        channel.claim(payer, 10e6, sig);
    }

    function test_claim_olderVoucherAfterNewerReverts() public {
        _deposit(FUND);
        bytes memory older = _voucher(provider, 10e6, 0);
        bytes memory sig3 = _voucher(provider, 20e6, 0);
        vm.prank(provider);
        channel.claim(payer, 20e6, sig3);

        vm.prank(provider);
        vm.expectRevert(abi.encodeWithSelector(AgentPayChannel.NotIncreasing.selector, 10e6, 20e6));
        channel.claim(payer, 10e6, older);
    }

    function test_claim_cannotExceedDeposit() public {
        _deposit(FUND);
        bytes memory sig = _voucher(provider, uint256(FUND) + 1, 0);
        vm.prank(provider);
        vm.expectRevert(
            abi.encodeWithSelector(AgentPayChannel.ExceedsDeposit.selector, uint256(FUND) + 1, FUND)
        );
        channel.claim(payer, uint256(FUND) + 1, sig);
    }

    function test_claim_wrongSignerReverts() public {
        _deposit(FUND);
        bytes memory forged = _voucherFrom(0xBAD, payer, provider, 10e6, 0);
        vm.prank(provider);
        vm.expectRevert(AgentPayChannel.InvalidSignature.selector);
        channel.claim(payer, 10e6, forged);
    }

    function test_claim_tamperedAmountReverts() public {
        _deposit(FUND);
        bytes memory sig = _voucher(provider, 10e6, 0);
        vm.prank(provider);
        vm.expectRevert(AgentPayChannel.InvalidSignature.selector);
        channel.claim(payer, 11e6, sig);
    }

    function test_claim_voucherIsBoundToItsProvider() public {
        _deposit(FUND);
        vm.prank(payer);
        channel.deposit(otherProvider, FUND);
        bytes memory forProvider = _voucher(provider, 10e6, 0);

        // The other provider cannot reuse a voucher made out to `provider`.
        vm.prank(otherProvider);
        vm.expectRevert(AgentPayChannel.InvalidSignature.selector);
        channel.claim(payer, 10e6, forProvider);
    }

    function test_claim_nonProviderGetsNothing() public {
        _deposit(FUND);
        bytes memory sig = _voucher(provider, 10e6, 0);
        address thief = makeAddr("thief");
        vm.prank(thief);
        vm.expectRevert(abi.encodeWithSelector(AgentPayChannel.ExceedsDeposit.selector, 10e6, 0));
        channel.claim(payer, 10e6, sig);
    }

    function test_claim_worksWhileCloseIsPending() public {
        _deposit(FUND);
        vm.prank(payer);
        channel.requestClose(provider);

        bytes memory sig4 = _voucher(provider, 50e6, 0);

        vm.prank(provider);
        channel.claim(payer, 50e6, sig4);
        assertEq(usdc.balanceOf(provider), 50e6);
    }

    // -------------------------------------------------------- close + withdraw

    function test_withdraw_refundsUnclaimedAfterDelay() public {
        _deposit(FUND);
        bytes memory sig5 = _voucher(provider, 30e6, 0);
        vm.prank(provider);
        channel.claim(payer, 30e6, sig5);

        vm.prank(payer);
        channel.requestClose(provider);
        vm.warp(block.timestamp + DELAY);
        vm.prank(payer);
        channel.withdraw(provider);

        assertEq(usdc.balanceOf(payer), 1_000e6 - 30e6);
        assertEq(usdc.balanceOf(provider), 30e6);
        assertEq(usdc.balanceOf(address(channel)), 0);
    }

    function test_withdraw_beforeDelayReverts() public {
        _deposit(FUND);
        vm.prank(payer);
        channel.requestClose(provider);
        uint64 closeAt = uint64(block.timestamp) + DELAY;

        vm.warp(block.timestamp + DELAY - 1);
        vm.prank(payer);
        vm.expectRevert(abi.encodeWithSelector(AgentPayChannel.CloseDelayPending.selector, closeAt));
        channel.withdraw(provider);
    }

    function test_withdraw_withoutCloseRequestReverts() public {
        _deposit(FUND);
        vm.prank(payer);
        vm.expectRevert(AgentPayChannel.NotClosing.selector);
        channel.withdraw(provider);
    }

    function test_requestClose_twiceReverts() public {
        _deposit(FUND);
        vm.startPrank(payer);
        channel.requestClose(provider);
        vm.expectRevert(AgentPayChannel.ChannelClosing.selector);
        channel.requestClose(provider);
        vm.stopPrank();
    }

    function test_requestClose_emptyChannelReverts() public {
        vm.prank(payer);
        vm.expectRevert(AgentPayChannel.ZeroAmount.selector);
        channel.requestClose(provider);
    }

    function test_withdraw_bumpsEpochAndKillsOldVouchers() public {
        _deposit(FUND);
        bytes memory oldVoucher = _voucher(provider, 10e6, 0);

        vm.prank(payer);
        channel.requestClose(provider);
        vm.warp(block.timestamp + DELAY);
        vm.prank(payer);
        channel.withdraw(provider);

        (,,, uint64 epoch) = _state(payer, provider);
        assertEq(epoch, 1);

        // Payer opens a fresh channel; the stale voucher must not drain it.
        _deposit(FUND);
        vm.prank(provider);
        vm.expectRevert(AgentPayChannel.InvalidSignature.selector);
        channel.claim(payer, 10e6, oldVoucher);

        // A voucher for the new epoch works.
        bytes memory sig6 = _voucher(provider, 10e6, 1);
        vm.prank(provider);
        channel.claim(payer, 10e6, sig6);
        assertEq(usdc.balanceOf(provider), 10e6);
    }

    function test_withdraw_afterEverythingClaimedRefundsNothing() public {
        _deposit(FUND);
        bytes memory sig7 = _voucher(provider, FUND, 0);
        vm.prank(provider);
        channel.claim(payer, FUND, sig7);
        vm.prank(payer);
        channel.requestClose(provider);
        vm.warp(block.timestamp + DELAY);
        vm.prank(payer);
        channel.withdraw(provider);
        assertEq(usdc.balanceOf(payer), 1_000e6 - FUND);
    }

    // -------------------------------------------------------------- isValidVoucher

    function test_isValidVoucher_tracksChannelState() public {
        _deposit(FUND);
        bytes memory sig = _voucher(provider, 10e6, 0);
        assertTrue(channel.isValidVoucher(payer, provider, 10e6, sig));
        // Wrong amount, over-deposit, and forged all fail.
        assertFalse(channel.isValidVoucher(payer, provider, 11e6, sig));
        assertFalse(
            channel.isValidVoucher(
                payer, provider, uint256(FUND) + 1, _voucher(provider, uint256(FUND) + 1, 0)
            )
        );
        assertFalse(
            channel.isValidVoucher(
                payer, provider, 10e6, _voucherFrom(0xBAD, payer, provider, 10e6, 0)
            )
        );

        // Already-claimed totals are no longer valid.
        vm.prank(provider);
        channel.claim(payer, 10e6, sig);
        assertFalse(channel.isValidVoucher(payer, provider, 10e6, sig));

        // A pending close makes new vouchers unacceptable.
        bytes memory next = _voucher(provider, 20e6, 0);
        assertTrue(channel.isValidVoucher(payer, provider, 20e6, next));
        vm.prank(payer);
        channel.requestClose(provider);
        assertFalse(channel.isValidVoucher(payer, provider, 20e6, next));
    }

    // ------------------------------------------------------------- smart wallets

    function test_smartWalletPayer_ERC1271() public {
        MockSmartWallet wallet = new MockSmartWallet(payer);
        usdc.mint(address(wallet), FUND);
        wallet.approve(usdc, address(channel), FUND);
        wallet.deposit(channel, provider, FUND);

        bytes32 digest = channel.voucherDigest(address(wallet), provider, 20e6, 0);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(payerKey, digest);
        bytes memory sig = abi.encode(r, s, v);

        assertTrue(channel.isValidVoucher(address(wallet), provider, 20e6, sig));
        vm.prank(provider);
        channel.claim(address(wallet), 20e6, sig);
        assertEq(usdc.balanceOf(provider), 20e6);
    }

    // --------------------------------------------------------------------- fuzz

    /// @dev Whatever vouchers are redeemed, tokens in equal funds still owed, and the provider
    ///      never receives more than the payer escrowed.
    function testFuzz_conservation(uint128 amount, uint128 c1, uint128 c2) public {
        amount = uint128(bound(amount, 1, 1_000e6));
        c1 = uint128(bound(c1, 1, amount));
        c2 = uint128(bound(c2, c1, amount));

        _deposit(amount);
        bytes memory sig8 = _voucher(provider, c1, 0);
        vm.prank(provider);
        channel.claim(payer, c1, sig8);
        if (c2 > c1) {
            bytes memory sig9 = _voucher(provider, c2, 0);
            vm.prank(provider);
            channel.claim(payer, c2, sig9);
        }

        uint256 paid = usdc.balanceOf(provider);
        assertEq(paid, c2);
        assertLe(paid, amount);
        assertEq(usdc.balanceOf(address(channel)), amount - paid);

        vm.prank(payer);
        channel.requestClose(provider);
        vm.warp(block.timestamp + DELAY);
        vm.prank(payer);
        channel.withdraw(provider);

        assertEq(usdc.balanceOf(address(channel)), 0);
        assertEq(usdc.balanceOf(payer) + usdc.balanceOf(provider), 1_000e6);
    }

    function testFuzz_cannotClaimMoreThanDeposited(uint128 amount, uint256 cumulative) public {
        amount = uint128(bound(amount, 1, 1_000e6));
        cumulative = bound(cumulative, uint256(amount) + 1, type(uint256).max);
        _deposit(amount);
        bytes memory sig = _voucher(provider, cumulative, 0);
        vm.prank(provider);
        vm.expectRevert(
            abi.encodeWithSelector(AgentPayChannel.ExceedsDeposit.selector, cumulative, amount)
        );
        channel.claim(payer, cumulative, sig);
    }
}
