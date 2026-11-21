// SPDX-License-Identifier: UNLICENSED
pragma solidity 0.8.28;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {EIP712} from "@openzeppelin/contracts/utils/cryptography/EIP712.sol";
import {SignatureChecker} from "@openzeppelin/contracts/utils/cryptography/SignatureChecker.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";

/// @title AgentPayChannel
/// @notice One-way payment channels from AI agents (payers) to service providers, in a single
///         ERC-20 such as USDC. A payer escrows funds, then pays per request by signing
///         off-chain vouchers with a growing cumulative total. The provider redeems the latest
///         voucher on-chain whenever it likes, so a thousand requests cost one transaction.
///         The payer cannot pull funds back until `closeDelay` has passed, which gives the
///         provider time to redeem. Payers can be EOAs or ERC-1271 smart wallets.
contract AgentPayChannel is EIP712, ReentrancyGuard {
    using SafeERC20 for IERC20;

    /// @dev State of one payer -> provider channel. Two storage slots.
    struct Channel {
        uint128 deposit; // total escrowed in the current epoch
        uint128 claimed; // total already paid to the provider in the current epoch
        uint64 closeAt; // 0 while open; otherwise when the payer may withdraw
        uint64 epoch; // bumped on withdrawal, which invalidates every earlier voucher
    }

    bytes32 public constant VOUCHER_TYPEHASH =
        keccak256("Voucher(address payer,address provider,uint256 cumulativeAmount,uint256 epoch)");

    IERC20 public immutable token;
    uint64 public immutable closeDelay;

    mapping(address payer => mapping(address provider => Channel)) public channels;

    event Deposited(address indexed payer, address indexed provider, uint256 amount, uint256 total);
    event Claimed(
        address indexed payer, address indexed provider, uint256 amount, uint256 cumulative
    );
    event CloseRequested(address indexed payer, address indexed provider, uint64 closeAt);
    event Withdrawn(address indexed payer, address indexed provider, uint256 amount);

    error ZeroAmount();
    error ZeroAddress();
    error ChannelClosing();
    error NotClosing();
    error CloseDelayPending(uint64 closeAt);
    error InvalidSignature();
    error NotIncreasing(uint256 cumulative, uint256 alreadyClaimed);
    error ExceedsDeposit(uint256 cumulative, uint256 deposit);
    error DepositOverflow();

    constructor(IERC20 token_, uint64 closeDelay_) EIP712("AgentPay", "1") {
        if (address(token_) == address(0)) revert ZeroAddress();
        token = token_;
        closeDelay = closeDelay_;
    }

    // ------------------------------------------------------------------ payer

    /// @notice Escrow `amount` for `provider`. The caller must have approved this contract.
    ///         Tops up an existing channel. Reverts while a close is pending.
    function deposit(address provider, uint128 amount) external nonReentrant {
        if (amount == 0) revert ZeroAmount();
        if (provider == address(0)) revert ZeroAddress();

        Channel storage ch = channels[msg.sender][provider];
        if (ch.closeAt != 0) revert ChannelClosing();

        uint256 total = uint256(ch.deposit) + amount;
        if (total > type(uint128).max) revert DepositOverflow();
        // Safe: checked against uint128 max just above.
        // forge-lint: disable-next-line(unsafe-typecast)
        ch.deposit = uint128(total);

        token.safeTransferFrom(msg.sender, address(this), amount);
        emit Deposited(msg.sender, provider, amount, total);
    }

    /// @notice Start the exit. After `closeDelay` the payer can withdraw what is unclaimed.
    ///         From now on the channel takes no deposits.
    function requestClose(address provider) external {
        Channel storage ch = channels[msg.sender][provider];
        if (ch.closeAt != 0) revert ChannelClosing();
        if (ch.deposit == 0) revert ZeroAmount();

        // Safe: a timestamp does not approach 2^64.
        // forge-lint: disable-next-line(unsafe-typecast)
        uint64 closeAt = uint64(block.timestamp) + closeDelay;
        ch.closeAt = closeAt;
        emit CloseRequested(msg.sender, provider, closeAt);
    }

    /// @notice Finish the exit and take back everything the provider has not claimed.
    ///         The epoch advances, so vouchers signed before this point are worthless.
    function withdraw(address provider) external nonReentrant {
        Channel storage ch = channels[msg.sender][provider];
        if (ch.closeAt == 0) revert NotClosing();
        if (block.timestamp < ch.closeAt) revert CloseDelayPending(ch.closeAt);

        uint256 refund = uint256(ch.deposit) - ch.claimed;
        uint64 nextEpoch = ch.epoch + 1;
        delete channels[msg.sender][provider];
        channels[msg.sender][provider].epoch = nextEpoch;

        if (refund != 0) token.safeTransfer(msg.sender, refund);
        emit Withdrawn(msg.sender, provider, refund);
    }

    // --------------------------------------------------------------- provider

    /// @notice Redeem a voucher. Pays the provider (the caller) the difference between the
    ///         voucher's cumulative total and what was claimed before. Anyone holding the
    ///         payer's signature can only ever pay the provider named in it.
    function claim(address payer, uint256 cumulativeAmount, bytes calldata signature)
        external
        nonReentrant
    {
        Channel storage ch = channels[payer][msg.sender];
        if (cumulativeAmount <= ch.claimed) revert NotIncreasing(cumulativeAmount, ch.claimed);
        if (cumulativeAmount > ch.deposit) revert ExceedsDeposit(cumulativeAmount, ch.deposit);
        if (!_validSignature(payer, msg.sender, cumulativeAmount, ch.epoch, signature)) {
            revert InvalidSignature();
        }

        uint256 payment = cumulativeAmount - ch.claimed;
        // Safe: cumulativeAmount <= ch.deposit, which is a uint128.
        // forge-lint: disable-next-line(unsafe-typecast)
        ch.claimed = uint128(cumulativeAmount);

        token.safeTransfer(msg.sender, payment);
        emit Claimed(payer, msg.sender, payment, cumulativeAmount);
    }

    // ------------------------------------------------------------------ views

    /// @notice Whether `signature` is a payer-signed voucher that `provider` could redeem right
    ///         now for `cumulativeAmount`. Providers call this before serving a request.
    ///         Returns false once the payer has asked to close.
    function isValidVoucher(
        address payer,
        address provider,
        uint256 cumulativeAmount,
        bytes calldata signature
    ) external view returns (bool) {
        Channel storage ch = channels[payer][provider];
        if (ch.closeAt != 0) return false;
        if (cumulativeAmount <= ch.claimed || cumulativeAmount > ch.deposit) return false;
        return _validSignature(payer, provider, cumulativeAmount, ch.epoch, signature);
    }

    /// @notice Escrowed funds the provider has not claimed yet.
    function unclaimed(address payer, address provider) external view returns (uint256) {
        Channel storage ch = channels[payer][provider];
        return uint256(ch.deposit) - ch.claimed;
    }

    /// @notice The EIP-712 digest a payer signs for a voucher.
    function voucherDigest(address payer, address provider, uint256 cumulativeAmount, uint256 epoch)
        public
        view
        returns (bytes32)
    {
        return _hashTypedDataV4(
            keccak256(abi.encode(VOUCHER_TYPEHASH, payer, provider, cumulativeAmount, epoch))
        );
    }

    function domainSeparator() external view returns (bytes32) {
        return _domainSeparatorV4();
    }

    function _validSignature(
        address payer,
        address provider,
        uint256 cumulativeAmount,
        uint256 epoch,
        bytes calldata signature
    ) private view returns (bool) {
        bytes32 digest = voucherDigest(payer, provider, cumulativeAmount, epoch);
        return SignatureChecker.isValidSignatureNow(payer, digest, signature);
    }
}
