defmodule VitrialSync.RejectionCodeTest do
  use ExUnit.Case, async: true

  alias VitrialSync.RejectionCode

  describe "the table, app/rejection_codes.py:65-72" do
    test "every class the push engine catches maps to its token" do
      # These four are the classes in `_apply_push_once`'s `except` clause
      # (app/sync_service.py:384-389), and therefore the only four that can
      # produce a rejection code on the path this app implements.
      assert RejectionCode.for_exception("InvalidMutation") == "invalid_mutation"
      assert RejectionCode.for_exception("AuthorizationRejected") == "authorization_rejected"
      assert RejectionCode.for_exception("LifecycleRejected") == "lifecycle_rejected"
      assert RejectionCode.for_exception("DeliveryExecutionRejected") == "delivery_rejected"
    end

    test "the two inner delivery classes also map to delivery_rejected" do
      # They are caught and re-raised as `DeliveryExecutionRejected` before they
      # reach the outcome stream (app/delivery_execution.py:605-608, 769-772), so
      # one token is what a client actually observes.
      assert RejectionCode.for_exception("DeliveryInstallationRejected") == "delivery_rejected"
      assert RejectionCode.for_exception("DeliveryProductionRejected") == "delivery_rejected"
    end

    test "an unmapped class is visibly unmapped rather than borrowing a real code" do
      assert RejectionCode.for_exception("SomethingNew") == "internal_error"
      assert RejectionCode.for_exception("") == "internal_error"
      assert RejectionCode.for_exception(nil) == "internal_error"
    end

    test "an explicit code at the raising site wins over the table" do
      # app/rejection_codes.py:57-59: `getattr(exc, "rejection_code", None)`
      # first. No exception in the current tree sets one, but the precedence is
      # part of the contract for when one does.
      assert RejectionCode.for_exception("AuthorizationRejected", "delete_blocked") == "delete_blocked"
      assert RejectionCode.for_exception("SomethingNew", "delete_blocked") == "delete_blocked"
    end

    test "an empty explicit code is ignored, matching `isinstance(explicit, str) and explicit`" do
      assert RejectionCode.for_exception("LifecycleRejected", "") == "lifecycle_rejected"
      assert RejectionCode.for_exception("LifecycleRejected", nil) == "lifecycle_rejected"
    end
  end

  describe "the vocabulary itself" do
    test "is closed, and every token the five decisions can return is in it" do
      assert RejectionCode.code?("authorization_rejected")
      assert RejectionCode.code?("lifecycle_rejected")
      assert RejectionCode.code?("delivery_rejected")
      assert RejectionCode.code?("committed")

      refute RejectionCode.code?("AuthorizationRejected")
      refute RejectionCode.code?("authorization")
      refute RejectionCode.code?(nil)
      refute RejectionCode.code?(:authorization_rejected)
    end

    test "lists all seventeen tokens from app/rejection_codes.py:19-47" do
      assert length(RejectionCode.codes()) == 17
      assert "rejected_mutation_id_reuse" in RejectionCode.codes()
      assert "internal_error" in RejectionCode.codes()
    end

    test "the helpers agree with the classifier" do
      assert RejectionCode.authorization_rejected() ==
               RejectionCode.for_exception("AuthorizationRejected")

      assert RejectionCode.lifecycle_rejected() ==
               RejectionCode.for_exception("LifecycleRejected")

      assert RejectionCode.delivery_rejected() ==
               RejectionCode.for_exception("DeliveryExecutionRejected")
    end
  end
end